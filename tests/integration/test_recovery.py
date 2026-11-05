from tests.fixtures.submission import submit_and_plan
import pytest

from robot_agent.domain.models import RobotEvent, TaskInput, TrustedContext


pytestmark = pytest.mark.integration
CONTEXT = TrustedContext("test-customer", "user", "test-user")
INPUT = TaskInput("req", "web", "test", "")


async def test_disconnect_unknown_keeps_slot(persisted_service, postgres_store):
    service, robot = persisted_service
    first = await submit_and_plan(service, CONTEXT, INPUT, "first")
    second = await submit_and_plan(service, CONTEXT, INPUT, "second")
    await service.dispatch_next("test-deployment")
    await service.on_robot_disconnected({"customer_id": CONTEXT.customer_id, "binding_id": "test-binding"}, 1)
    await service.schedule_pending("test-deployment")
    await service.dispatch_next("test-deployment")
    assert len(robot.sent) == 1
    assert (await service.get(CONTEXT, first.task_id)).reason_code == "RESULT_UNKNOWN"
    assert (await service.get(CONTEXT, second.task_id)).result_summary == ()
    assert await postgres_store.pool.fetchval("SELECT task_id FROM ra_execution_guard") == first.task_id


async def test_disconnect_result_resolves_unknown_without_repeating_step(persisted_service):
    service, robot = persisted_service
    view = await submit_and_plan(service, CONTEXT, INPUT, "key")
    await service.dispatch_next("test-deployment")
    await service.on_robot_disconnected({"customer_id": CONTEXT.customer_id, "binding_id": "test-binding"}, 1)
    await service.on_robot_event(RobotEvent("terminal", CONTEXT.customer_id, "test-binding", 1,
                                          robot.sent[0].command_id, "succeeded"))
    await service.dispatch_next("test-deployment")
    assert len(robot.sent) == 2
    assert [command.step_id for command in robot.sent] == ["s1", "s2"]
    assert (await service.get(CONTEXT, view.task_id)).reason_code == ""


async def test_late_conflict_quarantines_active_binding(persisted_service, postgres_store):
    service, robot = persisted_service
    old = await submit_and_plan(service, CONTEXT, INPUT, "old")
    await service.dispatch_next("test-deployment")
    await service.on_robot_event(RobotEvent("old-first", CONTEXT.customer_id, "test-binding", 1,
                                          robot.sent[0].command_id, "succeeded"))
    await service.dispatch_next("test-deployment")
    old_command = robot.sent[1]
    await service.on_robot_event(RobotEvent("old-last", CONTEXT.customer_id, "test-binding", 1,
                                          old_command.command_id, "succeeded"))
    new = await submit_and_plan(service, CONTEXT, INPUT, "new")
    await service.dispatch_next("test-deployment")
    await service.on_robot_event(RobotEvent("conflict", CONTEXT.customer_id, "test-binding", 1,
                                          old_command.command_id, "failed"))
    assert (await service.get(CONTEXT, old.task_id)).state == "SUCCEEDED"
    assert (await service.get(CONTEXT, old.task_id)).result_disputed
    current = await service.get(CONTEXT, new.task_id)
    assert current.reason_code == "EVIDENCE_CONFLICT"
    assert current.state == "BLOCKED"
    assert await postgres_store.pool.fetchval("SELECT task_id FROM ra_execution_guard") == new.task_id
    assert await postgres_store.pool.fetchval("SELECT isolation_reason FROM ra_robot_bindings") == "EVIDENCE_CONFLICT"


async def test_unchanged_recover_is_idempotent(persisted_service):
    service, _ = persisted_service
    view = await submit_and_plan(service, CONTEXT, INPUT, "key")
    await service.dispatch_next("test-deployment")
    await service.recover("test-deployment")
    first = await service.get(CONTEXT, view.task_id)
    await service.recover("test-deployment")
    assert await service.get(CONTEXT, view.task_id) == first


async def test_prepared_command_is_preserved_but_never_sent_after_restart(persisted_service, postgres_store):
    import asyncpg
    from tests.integration.test_command_transactions import install_deferred_failure
    service, robot = persisted_service
    await submit_and_plan(service, CONTEXT, INPUT, "key")
    await install_deferred_failure(postgres_store, "NEW.dispatch_state='DISPATCHING'")
    with pytest.raises(asyncpg.CheckViolationError):
        await service.dispatch_next("test-deployment")
    assert robot.sent == []
    command_id = await postgres_store.pool.fetchval("SELECT command_id FROM ra_commands")
    await postgres_store.pool.execute("DROP TRIGGER test_command_commit ON ra_commands")
    await service.recover("test-deployment")
    await service.schedule_pending("test-deployment")
    await service.dispatch_next("test-deployment")
    assert not robot.sent
    assert await postgres_store.pool.fetchval("SELECT command_id FROM ra_commands") == command_id
    assert await postgres_store.pool.fetchval("SELECT reason_code FROM ra_tasks") == "RESTART_TASK_LOST"
    assert await postgres_store.pool.fetchval("SELECT count(*) FROM ra_commands") == 1


async def test_policy_rechecked_between_prepare_and_dispatch(persisted_service):
    service, robot = persisted_service
    await submit_and_plan(service, CONTEXT, INPUT, "key")
    original = service.policy.evaluate
    checks = 0

    async def changed_policy(context, plan, approvals):
        nonlocal checks
        checks += 1
        return await original(context, plan, approvals) if checks == 1 else "deny"

    service.policy.evaluate = changed_policy
    await service.dispatch_next("test-deployment")
    assert robot.sent == []
