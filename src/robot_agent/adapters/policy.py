"""Explicit per-customer policy; absence is unavailable, never implicit permission."""

from robot_agent.domain.models import TrustedContext


class ConfiguredPolicy:
    def __init__(self, identity, *, version, actions, approval_ttl_seconds):
        self.identity = identity
        self.version = version
        self.actions = actions
        self.approval_ttl_seconds = approval_ttl_seconds

    async def evaluate(self, context, plan, approvals):
        if not self.version or self.identity.current_actor(context) is None:
            return "unavailable"
        configured = self.actions.get(context.customer_id, {})
        results = [configured.get(step.action) for step in plan.steps]
        if "deny" in results:
            return "deny"
        if not results or any(result not in {"allow", "require_confirmation"} for result in results):
            return "unavailable"
        return "require_confirmation" if "require_confirmation" in results else "allow"

    async def can_approve(self, context, task):
        actor = self.identity.current_actor(context)
        return (actor is not None and task.customer_id == context.customer_id
                and "approver" in actor.get("roles", []))

    async def can_control(self, context, owner):
        """Task origin stays immutable; customer users may manage their robots' conversations."""
        if (context.actor_kind not in {"user", "robot"} or owner.customer_id != context.customer_id
                or self.identity.current_actor(context) is None):
            return False
        if (owner.actor_kind, owner.actor_id) == (context.actor_kind, context.actor_id):
            return True
        if context.actor_kind != "user" or owner.actor_kind != "robot":
            return False
        robot = TrustedContext(owner.customer_id, "robot", owner.actor_id)
        return self.identity.current_actor(robot) is not None

    async def can_cancel(self, context, task):
        return await self.can_control(context, task)
