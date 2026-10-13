from datetime import datetime, timezone

import pytest

from robot_agent.adapters.identity import ConfiguredIdentity
from robot_agent.domain.models import TaskInput, TrustedContext


pytestmark = pytest.mark.integration


async def test_identity_credential_binds_customer_and_robot():
    identity = ConfiguredIdentity(users={"test-user-token": {"customer_id": "c", "actor_id": "user", "roles": ["user"]}},
                                  robots={"test-robot-token": {"customer_id": "c", "binding_id": "b", "wire_robot_id": "r"}})
    context = await identity.resolve_web("test-user-token")
    assert context.customer_id == "c"
    assert context.actor_id == "user"
    with pytest.raises(PermissionError):
        await identity.resolve_robot("test-robot-token", "other-robot")
    with pytest.raises(PermissionError):
        await identity.resolve_web("test-robot-token")


async def test_current_deny_overrides_approval(persisted_service):
    service, robot = persisted_service
    context = TrustedContext("test-customer", "user", "test-user")
    service.policy.result = "require_confirmation"
    view = await service.submit(context, TaskInput("req", "web", "test", ""), "key")
    approved = await service.decide(context, view.task_id, view.version, "approve", "decision-key")
    service.policy.result = "deny"
    await service.dispatch_next("test-deployment")
    assert robot.sent == []
    assert approved.plan_id == view.plan_id


async def test_revoked_approval_prevents_dispatch(persisted_service, postgres_store):
    service, robot = persisted_service
    context = TrustedContext("test-customer", "user", "test-user")
    service.policy.result = "require_confirmation"
    view = await service.submit(context, TaskInput("req", "web", "test", ""), "key")
    await service.decide(context, view.task_id, view.version, "approve", "decision-key")
    await postgres_store.pool.execute("UPDATE ra_decisions SET revoked_at=$1 WHERE kind='approval'", datetime.now(timezone.utc))
    await service.dispatch_next("test-deployment")
    assert robot.sent == []


async def test_changed_resource_blocks_without_replacing_planned_target(persisted_service):
    from dataclasses import replace
    service, robot = persisted_service
    context = TrustedContext("test-customer", "user", "test-user")
    view = await service.submit(context, TaskInput("req", "web", "test", ""), "key")
    original = service.resources.snapshot

    async def changed_snapshot(context):
        return replace(await original(context), waypoints={"test-table": "environment:v2:table"})

    service.resources.snapshot = changed_snapshot
    await service.dispatch_next("test-deployment")
    result = await service.get(context, view.task_id)
    assert result.reason_code == "RESOURCE_VERSION_CHANGED"
    assert robot.sent == []


async def test_approval_cannot_clear_unknown_result(persisted_service):
    from robot_agent.domain.models import ConflictError
    service, robot = persisted_service
    context = TrustedContext("test-customer", "user", "test-user")
    view = await service.submit(context, TaskInput("req", "web", "test", ""), "key")
    await service.dispatch_next("test-deployment")
    await service.recover("test-deployment")
    blocked = await service.get(context, view.task_id)
    with pytest.raises(ConflictError):
        await service.decide(context, view.task_id, blocked.version, "approve", "decision-key")
    assert (await service.get(context, view.task_id)).reason_code == "RESULT_UNKNOWN"
    assert len(robot.sent) == 1
