"""Combine pure plan validation with the injected authorization boundary."""

from dataclasses import dataclass
from uuid import uuid4

from robot_agent.domain.models import Decision, PlanError, ValidationResult
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
        validated = validate_plan(candidate, snapshot, self.catalog)
        result = await self.policy.evaluate(context, candidate, list(approvals))
        if result not in {"allow", "deny", "require_confirmation", "unavailable"}:
            result = "unavailable"
        records = (
            Decision(str(uuid4()), "validation", "valid", "serial-grounding:v1", (snapshot.snapshot_id,)),
            Decision(str(uuid4()), "authorization", result, getattr(self.policy, "version", ""),
                     (snapshot.snapshot_id,), {"phase": phase, "actor_id": context.actor_id}),
        )
        return CheckResult(validated, result, records)
