"""Explicit per-customer policy; absence is unavailable, never implicit permission."""


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
