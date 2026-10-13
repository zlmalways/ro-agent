"""Customer-owned, immutable capability and environment documents."""

from datetime import datetime, timedelta, timezone
import json
from uuid import uuid4

from robot_agent.domain.models import ConflictError, ContextSnapshot, DependencyUnavailable, PlanError


class Resources:
    def __init__(self, store, *, freshness_seconds, max_entries):
        self.store = store
        self.freshness_seconds = freshness_seconds
        self.max_entries = max_entries

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

    async def read(self, context, kind):
        async with self.store.system_unit_of_work() as tx:
            bindings = await tx.customer_bindings(context.customer_id)
            if len(bindings) != 1:
                raise DependencyUnavailable("ROBOT_BINDING_UNAVAILABLE")
            return await tx.current_resource(context.customer_id, bindings[0]["binding_id"], kind)

    async def binding_for(self, context):
        async with self.store.system_unit_of_work() as tx:
            bindings = await tx.customer_bindings(context.customer_id)
            if len(bindings) != 1:
                raise DependencyUnavailable("ROBOT_BINDING_UNAVAILABLE")
            return bindings[0]["binding_id"]

    async def snapshot(self, context):
        async with self.store.system_unit_of_work() as tx:
            bindings = await tx.customer_bindings(context.customer_id)
            if len(bindings) != 1:
                raise DependencyUnavailable("ROBOT_BINDING_UNAVAILABLE")
            binding = bindings[0]
            environment = await tx.current_resource(context.customer_id, binding["binding_id"], "environment")
            capability = await tx.current_resource(context.customer_id, binding["binding_id"], "capability")
        observations = json.loads(binding["observations"])
        generation = binding["connection_generation"]
        now = datetime.now(timezone.utc)

        def fresh(field):
            evidence = observations.get(field)
            if not isinstance(evidence, dict) or evidence.get("connection_generation") != generation:
                return None
            instant = datetime.fromisoformat(evidence["received_at"])
            return evidence.get("value") if 0 <= (now - instant).total_seconds() <= self.freshness_seconds else None

        connected = observations.get("connected", {}).get("value") is True
        declaration = observations.get("skills", {})
        skills = frozenset(declaration.get("value", ())) if connected and not binding["isolation_reason"] \
            and declaration.get("connection_generation") == generation else frozenset()
        waypoints, skill_refs = {}, {}
        if environment:
            for name in fresh("waypoint_names") or ():
                if isinstance(name, str) and name in environment["entries"]:
                    waypoints[name] = f"environment:{binding['binding_id']}:{environment['revision']}:{name}"
        if capability:
            for item in fresh("brain_skills") or ():
                identifier = item.get("skill_id") if isinstance(item, dict) else item
                if isinstance(identifier, str) and identifier in capability["entries"]:
                    skill_refs[identifier] = f"capability:{binding['binding_id']}:{capability['revision']}:{identifier}"
        readiness = {field: fresh(field) for field in ("nav_server_ready", "brain_server_ready", "brain_engine", "busy")
                     if fresh(field) is not None}
        deadline_fields = tuple(readiness) + ("waypoint_names", "brain_skills")
        deadlines = [datetime.fromisoformat(observations[field]["received_at"]) + timedelta(seconds=self.freshness_seconds)
                     for field in deadline_fields if fresh(field) is not None]
        return ContextSnapshot(context.customer_id, binding["binding_id"], generation, skills, waypoints, skill_refs,
                               readiness, str(uuid4()), frozenset(binding["allowed_capabilities"]), observations,
                               capability["entries"] if capability else {}, environment["entries"] if environment else {}, binding["version"],
                               min(deadlines) if deadlines else None)
