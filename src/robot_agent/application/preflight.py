"""Combine pure plan validation with the injected authorization boundary."""

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from uuid import uuid4

from robot_agent.domain.models import CandidatePlan, CandidateStep, Decision, Plan, PlanError, PreflightBlocked, ReadinessBlocked, ValidationResult
from robot_agent.domain.validation import validate_plan


_MISSING = object()


def _readiness_failure(snapshot, step, field, expected):
    evidence = snapshot.readiness_evidence.get(field) or {}
    actual = snapshot.readiness.get(field, _MISSING)
    age = None
    if snapshot.captured_at and evidence.get("received_at"):
        age = (datetime.fromisoformat(snapshot.captured_at)
               - datetime.fromisoformat(evidence["received_at"])).total_seconds()
    if actual is not _MISSING:
        category = "reported_not_ready"
    elif (evidence.get("connection_generation") == snapshot.generation
          and age is not None and snapshot.freshness_seconds is not None and age > snapshot.freshness_seconds):
        category = "report_expired"
    else:
        category = "no_valid_current_report"
    return {
        "category": category,
        "field": field,
        "expected": expected,
        "actual": None if actual is _MISSING else actual,
        "reported_value": evidence.get("value"),
        "reported_at": evidence.get("received_at"),
        "reported_generation": evidence.get("connection_generation"),
        "age_seconds_at_capture": age,
        "step_id": step.step_id,
        "action": step.action,
        "target": step.args.get("location") if step.action == "move_to" else step.args.get("skill_id"),
    }


@dataclass(frozen=True)
class CheckResult:
    validation: ValidationResult
    authorization: str
    records: tuple[Decision, ...]

    def with_approval_settings(self, *, enabled, version):
        """Only confirmation can be waived; policy denial/unavailability stay intact."""
        record = self.records[-1]
        result = "allow" if enabled is True and self.authorization == "require_confirmation" else self.authorization
        record = replace(record, result=result, detail={
            **record.detail, "approval_bypass": enabled, "approval_bypass_version": version,
        })
        return replace(self, authorization=result, records=self.records[:-1] + (record,))


class Preflight:
    def __init__(self, catalog, policy):
        self.catalog = catalog
        self.policy = policy

    async def check(self, context, candidate, snapshot, phase="admission", approvals=(),
                    require_fresh_approval=True):
        if snapshot.customer_id != context.customer_id:
            raise PlanError("snapshot customer mismatch")
        plan = candidate if isinstance(candidate, Plan) else None
        value = CandidatePlan("plan", tuple(CandidateStep(step.step_id, step.action, step.args, step.depends_on)
                                             for step in plan.steps)) if plan else candidate
        if not snapshot.robot_binding_id or not snapshot.skills:
            raise PreflightBlocked("CAPABILITY_UNKNOWN")
        # Planning may take longer than the freshness window.  The snapshot is
        # still used to ground the candidate, while dispatch/approval/write
        # phases re-read the current snapshot and enforce its deadline.
        if phase != "planning" and snapshot.valid_until is not None and datetime.now(timezone.utc) >= snapshot.valid_until:
            raise PreflightBlocked("RESOURCE_EXPIRED", source="resources", phase=phase)
        validated = validate_plan(value, snapshot, self.catalog)
        if plan and validated.steps != plan.steps:
            raise PreflightBlocked("RESOURCE_VERSION_CHANGED")
        readiness_failures = []
        for step in validated.steps:
            if step.action == "move_to" and snapshot.readiness.get("nav_server_ready") is not True:
                readiness_failures.append(_readiness_failure(snapshot, step, "nav_server_ready", True))
            if step.action == "vla_skill":
                if snapshot.readiness.get("brain_server_ready") is not True:
                    readiness_failures.append(_readiness_failure(snapshot, step, "brain_server_ready", True))
                if snapshot.readiness.get("brain_engine") != "ready":
                    readiness_failures.append(_readiness_failure(snapshot, step, "brain_engine", "ready"))
        if readiness_failures:
            raise ReadinessBlocked(error_id=str(uuid4()), phase=phase, details={
                "checks": readiness_failures, "robot_binding_id": snapshot.robot_binding_id,
                "current_generation": snapshot.generation, "captured_at": snapshot.captured_at,
                "freshness_seconds": snapshot.freshness_seconds,
                "checked_at": datetime.now(timezone.utc).isoformat(),
            })
        result = await self.policy.evaluate(context, candidate, list(approvals))
        if result not in {"allow", "deny", "require_confirmation", "unavailable"}:
            result = "unavailable"
        base_result = result
        if result == "require_confirmation" and plan and any(approval_covers(
                evidence, customer_id=context.customer_id, plan_id=plan.plan_id, revision=plan.revision,
                scope=tuple(step.step_id for step in plan.steps), policy_version=getattr(self.policy, "version", ""),
                now=datetime.now(timezone.utc), require_fresh=require_fresh_approval) for evidence in approvals):
            result = "allow"
        records = (
            Decision(str(uuid4()), "validation", "valid", "serial-grounding:v1", (snapshot.snapshot_id,)),
            Decision(str(uuid4()), "authorization", result, getattr(self.policy, "version", ""),
                     (snapshot.snapshot_id,), {"phase": phase, "actor_id": context.actor_id, "base_result": base_result}),
        )
        check = CheckResult(validated, result, records)
        if plan:
            # Use this plan's captured setting, never the robot's current switch.
            check = check.with_approval_settings(enabled=plan.approval_bypass, version=plan.approval_bypass_version)
        return check


def approval_covers(evidence, *, customer_id, plan_id, revision, scope, policy_version, now,
                    require_fresh=True):
    return (evidence["customer_id"] == customer_id and evidence["plan_id"] == plan_id
            and evidence["plan_revision"] == revision and set(scope).issubset(evidence["scope"])
            and evidence["policy_version"] == policy_version and evidence["revoked_at"] is None
            and (not require_fresh or evidence["issued_at"] <= now < evidence["expires_at"]))
