"""Customer-owned, immutable capability and environment documents."""

from datetime import datetime, timedelta, timezone
import json
from uuid import uuid4

from robot_agent.domain.models import ConflictError, ContextSnapshot, DependencyUnavailable, PlanError

from robot_agent.ports import ConnectedRobots
from robot_agent.domain.models import RobotDetail, RobotSummary
from robot_agent.domain.robot_report import REPORT_FIELDS, RobotReportError, brain_skill_id, validate_report_value


def _observations(binding):
    try:
        observations = json.loads(binding["observations"])
    except json.JSONDecodeError:
        raise RobotReportError("observations") from None
    if not isinstance(observations, dict):
        raise RobotReportError("observations")
    for field, evidence in observations.items():
        if field not in REPORT_FIELDS:
            continue
        if not isinstance(evidence, dict) or not {"value", "received_at", "connection_generation"} <= evidence.keys():
            raise RobotReportError(field)
        validate_report_value(field, evidence["value"])
        if type(evidence["connection_generation"]) is not int or evidence["connection_generation"] < 0:
            raise RobotReportError(field + ".connection_generation")
        if not isinstance(evidence["received_at"], str):
            raise RobotReportError(field + ".received_at")
        try:
            instant = datetime.fromisoformat(evidence["received_at"])
        except ValueError:
            raise RobotReportError(field + ".received_at") from None
        if instant.tzinfo is None:
            raise RobotReportError(field + ".received_at")
        if field == "waypoint_names" and "reported_map" in evidence and evidence["reported_map"] is not None \
                and not isinstance(evidence["reported_map"], str):
            raise RobotReportError(field + ".reported_map")
    return observations


def _waypoints_match_map(places, current_map):
    if "reported_map" in places:
        return places["reported_map"] == current_map["value"]
    # Old readiness frames stamped every field in a single report with the same time.
    return places["received_at"] == current_map["received_at"]


class Resources:
    def __init__(self, store, *, freshness_seconds, max_entries, use_reported_waypoints=False,
                 connected_robots: ConnectedRobots | None = None, missing_credentials=frozenset()):
        self.store = store
        self.freshness_seconds = freshness_seconds
        self.max_entries = max_entries
        self.use_reported_waypoints = use_reported_waypoints
        self.connected_robots = connected_robots
        self.missing_credentials = frozenset(missing_credentials)

    async def put(self, context, binding_id, kind, entries, source_refs, *, base_version=None):
        if "maintainer" not in context.roles:
            raise PermissionError("RESOURCE_WRITE_DENIED")
        if kind not in {"environment", "capability"} or not isinstance(entries, dict) or not entries or len(entries) > self.max_entries:
            raise PlanError("invalid resource entries")
        if any(not isinstance(key, str) or not key or not isinstance(value, str) or not value.strip()
               for key, value in entries.items()) or not source_refs or any(not isinstance(ref, str) or not ref for ref in source_refs):
            raise PlanError("resource labels and provenance required")
        async with self.store.system_unit_of_work() as tx:
            binding = await tx.get_binding(context.customer_id, binding_id)
            if binding is None:
                raise PermissionError("RESOURCE_BINDING_DENIED")
            current = await tx.current_resource(context.customer_id, binding_id, kind)
            if base_version is not None and base_version != (current["revision"] if current else 0):
                raise ConflictError("resource version changed")
            return await tx.put_resource(context, binding_id, kind, entries, source_refs)

    async def _default_binding(self, tx, context):
        """Compatibility for the old single-robot resource endpoints only."""
        bindings = await tx.customer_bindings(context.customer_id)
        if len(bindings) != 1:
            raise DependencyUnavailable("ROBOT_BINDING_UNAVAILABLE")
        return bindings[0]

    async def read(self, context, kind, binding_id=None):
        async with self.store.system_unit_of_work() as tx:
            binding = await self._default_binding(tx, context) if binding_id is None else await tx.get_binding(context.customer_id, binding_id)
            if binding is None:
                raise PermissionError("RESOURCE_BINDING_DENIED")
            return await tx.current_resource(context.customer_id, binding["binding_id"], kind)

    async def binding_for(self, context):
        async with self.store.system_unit_of_work() as tx:
            return (await self._default_binding(tx, context))["binding_id"]

    async def online_robot_details(self, context):
        if self.connected_robots is None:
            return []
        connections = self.connected_robots(context.customer_id)
        snapshots = []
        unavailable = []
        for binding_id, generation in connections.items():
            try:
                snapshot = await self.snapshot(context, binding_id)
            except DependencyUnavailable as exc:
                if not isinstance(exc, RobotReportError) and str(exc) != "ROBOT_BINDING_UNAVAILABLE":
                    raise
                unavailable.append(exc)
                continue
            if snapshot.generation == generation:
                snapshots.append(snapshot)
        # Database reads yield control: a connection may have disappeared or been replaced.
        current = self.connected_robots(context.customer_id)
        if not snapshots and unavailable:
            raise unavailable[0]
        return [snapshot for snapshot in snapshots
                if current.get(snapshot.robot_binding_id) == snapshot.generation]

    async def snapshot(self, context, binding_id=None):
        async with self.store.system_unit_of_work() as tx:
            if binding_id is None:
                binding = await self._default_binding(tx, context)
            else:
                binding = await tx.get_binding(context.customer_id, binding_id)
            if binding is None or not binding["enabled"] or binding["isolation_reason"]:
                raise DependencyUnavailable("ROBOT_BINDING_UNAVAILABLE")
            environment = await tx.current_resource(context.customer_id, binding["binding_id"], "environment")
            capability = await tx.current_resource(context.customer_id, binding["binding_id"], "capability")
        return await self._snapshot(context, binding, environment, capability)

    async def _snapshot(self, context, binding, environment, capability):
        observations = _observations(binding)
        generation = binding["connection_generation"]
        now = datetime.now(timezone.utc)

        def fresh(field):
            evidence = observations.get(field)
            if not isinstance(evidence, dict) or evidence.get("connection_generation") != generation:
                return None
            instant = datetime.fromisoformat(evidence["received_at"])
            return evidence.get("value") if 0 <= (now - instant).total_seconds() <= self.freshness_seconds else None

        connected = observations.get("connected", {}).get("value") is True
        if self.connected_robots is not None:
            connected = connected and self.connected_robots(context.customer_id).get(binding["binding_id"]) == generation
        declaration = observations.get("skills", {})
        skills = frozenset(declaration.get("value", ())) if connected and not binding["isolation_reason"] \
            and declaration.get("connection_generation") == generation else frozenset()
        waypoints, skill_refs = {}, {}
        names = fresh("waypoint_names")
        environment_labels = environment["entries"] if environment else {}
        if self.use_reported_waypoints:
            map_name = fresh("map")
            if (connected and not binding["isolation_reason"] and isinstance(map_name, str) and map_name.strip()
                    and isinstance(names, list) and names):
                if _waypoints_match_map(observations["waypoint_names"], observations["map"]):
                    for name in names:
                        if isinstance(name, str) and name.strip():
                            waypoints[name] = f"robot-waypoint:{binding['binding_id']}:{generation}:{map_name}:{name}"
            environment_labels = {name: environment_labels[name] for name in waypoints if name in environment_labels}
        elif environment:
            for name in names or ():
                if isinstance(name, str) and name in environment["entries"]:
                    waypoints[name] = f"environment:{binding['binding_id']}:{environment['revision']}:{name}"
        if capability:
            for item in fresh("brain_skills") or ():
                identifier = brain_skill_id(item)
                if isinstance(identifier, str) and identifier in capability["entries"]:
                    skill_refs[identifier] = f"capability:{binding['binding_id']}:{capability['revision']}:{identifier}"
        readiness = {field: fresh(field) for field in ("nav_server_ready", "brain_server_ready", "brain_engine", "busy")
                     if fresh(field) is not None}
        deadline_fields = tuple(readiness) + ("waypoint_names", "brain_skills")
        if self.use_reported_waypoints and waypoints:
            deadline_fields += ("map",)
        deadlines = [datetime.fromisoformat(observations[field]["received_at"]) + timedelta(seconds=self.freshness_seconds)
                     for field in deadline_fields if fresh(field) is not None]
        return ContextSnapshot(context.customer_id, binding["binding_id"], generation, skills, waypoints, skill_refs,
                               readiness, str(uuid4()), frozenset(binding["allowed_capabilities"]), observations,
                               capability["entries"] if capability else {}, environment_labels, binding["version"],
                               min(deadlines) if deadlines else None)

    async def list_robots(self, context):
        async with self.store.system_unit_of_work() as tx:
            bindings = await tx.customer_bindings(context.customer_id, include_disabled=True)
        return [(await self.robot_detail(context, row["binding_id"])).robot for row in bindings]

    async def robot_detail(self, context, binding_id):
        async with self.store.system_unit_of_work() as tx:
            binding = await tx.get_binding(context.customer_id, binding_id)
            if binding is None:
                raise PermissionError("RESOURCE_BINDING_DENIED")
            environment = await tx.current_resource(context.customer_id, binding_id, "environment")
            capability = await tx.current_resource(context.customer_id, binding_id, "capability")
        issues = []
        if not binding["enabled"]:
            issues.append("DISABLED")
        if binding["isolation_reason"]:
            issues.append("ISOLATED")
        if (context.customer_id, binding_id) in self.missing_credentials:
            issues.append("CREDENTIAL_MISSING")
        snapshot = None
        last_report = {}
        try:
            snapshot = await self._snapshot(context, binding, environment, capability)
            last_report = {key: value for key, value in snapshot.readiness_evidence.items() if key in REPORT_FIELDS}
        except RobotReportError:
            issues.append("REPORT_INVALID")
        connected = self.connected_robots is not None and \
            self.connected_robots(context.customer_id).get(binding_id) == binding["connection_generation"]
        if not connected:
            issues.append("OFFLINE")
        if issues:
            snapshot = None
        summary = RobotSummary(binding_id, binding["wire_robot_id"], binding["enabled"], connected,
                               binding["isolation_reason"], binding["connection_generation"], tuple(issues))
        return RobotDetail(summary, snapshot, last_report, environment, capability)
