"""Combine pure plan validation with the injected authorization boundary."""

from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import uuid4

from robot_agent.domain.models import CandidatePlan, CandidateStep, Decision, Plan, PlanError, PreflightBlocked, ValidationResult
from robot_agent.domain.validation import validate_plan


@dataclass(frozen=True)
class CheckResult:
    validation: ValidationResult
    authorization: str
    records: tuple[Decision, ...]


class Preflight:
    def __init__(self, catalog, policy):
        self.catalog = catalog
        self.policy = policy

    async def check(self, context, candidate, snapshot, phase="admission", approvals=()):
        if snapshot.customer_id != context.customer_id:
            raise PlanError("snapshot customer mismatch")
        plan = candidate if isinstance(candidate, Plan) else None
        value = CandidatePlan("plan", tuple(CandidateStep(step.step_id, step.action, step.args, step.depends_on)
                                             for step in plan.steps)) if plan else candidate
        if not snapshot.robot_binding_id or not snapshot.skills:
            raise PreflightBlocked("CAPABILITY_UNKNOWN")
        validated = validate_plan(value, snapshot, self.catalog)
        if plan and validated.steps != plan.steps:
            raise PreflightBlocked("RESOURCE_VERSION_CHANGED")
        for step in validated.steps:
            if step.action == "move_to" and snapshot.readiness.get("nav_server_ready") is not True:
                raise PreflightBlocked("READINESS_UNKNOWN")
            if step.action == "vla_skill" and (snapshot.readiness.get("brain_server_ready") is not True
                                              or snapshot.readiness.get("brain_engine") != "ready"):
                raise PreflightBlocked("READINESS_UNKNOWN")
        result = await self.policy.evaluate(context, candidate, list(approvals))
        if result not in {"allow", "deny", "require_confirmation", "unavailable"}:
            result = "unavailable"
        if result == "require_confirmation" and plan and any(approval_covers(
                evidence, customer_id=context.customer_id, plan_id=plan.plan_id, revision=plan.revision,
                scope=tuple(step.step_id for step in plan.steps), policy_version=getattr(self.policy, "version", ""),
                now=datetime.now(timezone.utc)) for evidence in approvals):
            result = "allow"
        records = (
            Decision(str(uuid4()), "validation", "valid", "serial-grounding:v1", (snapshot.snapshot_id,)),
            Decision(str(uuid4()), "authorization", result, getattr(self.policy, "version", ""),
                     (snapshot.snapshot_id,), {"phase": phase, "actor_id": context.actor_id}),
        )
        return CheckResult(validated, result, records)


def approval_covers(evidence, *, customer_id, plan_id, revision, scope, policy_version, now):
    return (evidence["customer_id"] == customer_id and evidence["plan_id"] == plan_id
            and evidence["plan_revision"] == revision and set(scope).issubset(evidence["scope"])
            and evidence["policy_version"] == policy_version and evidence["revoked_at"] is None
            and evidence["issued_at"] <= now < evidence["expires_at"])
