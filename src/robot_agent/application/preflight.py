"""Combine pure plan validation with the injected authorization boundary."""

from dataclasses import dataclass

from robot_agent.domain.models import PlanError, ValidationResult
from robot_agent.domain.validation import validate_plan


@dataclass(frozen=True)
class CheckResult:
    validation: ValidationResult
    authorization: str


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
        return CheckResult(validated, result)
