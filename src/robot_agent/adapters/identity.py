"""Explicit credential identities; no identity inferred from task text or robot hello."""

from hmac import compare_digest

from robot_agent.domain.models import DependencyUnavailable, TrustedContext


class ConfiguredIdentity:
    def __init__(self, *, users, robots):
        self.users = users
        self.robots = robots

    async def resolve_web(self, token):
        if not self.users:
            raise DependencyUnavailable("IDENTITY_UNAVAILABLE")
        record = next((row for secret, row in self.users.items() if token and compare_digest(secret, token)), None)
        if record is None:
            raise PermissionError("UNAUTHENTICATED")
        return TrustedContext(record["customer_id"], "user", record["actor_id"], frozenset(record["roles"]), "configured-credential")

    async def resolve_robot(self, token, robot_id):
        if not self.robots:
            raise DependencyUnavailable("IDENTITY_UNAVAILABLE")
        record = next((row for secret, row in self.robots.items() if token and compare_digest(secret, token)), None)
        if record is None or robot_id not in {record["wire_robot_id"], record.get("voice_wire_robot_id")}:
            raise PermissionError("UNAUTHENTICATED")
        return {**record, "channel": "voice" if robot_id == record.get("voice_wire_robot_id") else "command",
                "wire_robot_id": robot_id}

    def current_actor(self, context):
        if context.actor_kind == "user":
            return next((row for row in self.users.values() if row["customer_id"] == context.customer_id
                         and row["actor_id"] == context.actor_id), None)
        return next((row for row in self.robots.values() if row["customer_id"] == context.customer_id
                     and row["binding_id"] == context.actor_id), None)
