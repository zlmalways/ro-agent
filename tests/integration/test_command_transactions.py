"""Real commits/rollbacks; the simulated robot stays under tests/fixtures."""

import asyncio

import asyncpg
import pytest

from robot_agent.domain.models import ConflictError, RobotEvent, TaskInput, TrustedContext


pytestmark = pytest.mark.integration
CONTEXT = TrustedContext("test-customer", "user", "test-user")
INPUT = TaskInput("req", "web", "test: fetch cup", "")


async def install_deferred_failure(store, condition):
    # Inject a real COMMIT-time failure, rather than replacing the store with a mock.
    await store.pool.execute(f"""
        CREATE FUNCTION test_fail_command_commit() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF {condition} THEN
                RAISE EXCEPTION 'test commit rejected' USING ERRCODE = '23514';
            END IF;
            RETURN NEW;
        END $$;
        CREATE CONSTRAINT TRIGGER test_command_commit
        AFTER INSERT OR UPDATE ON ra_commands DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION test_fail_command_commit();
    """)


@pytest.mark.parametrize("dispatch_state", ["PREPARED", "DISPATCHING"])
async def test_failed_commit_never_sends(persisted_service, postgres_store, dispatch_state):
    service, robot = persisted_service
    await service.submit(CONTEXT, INPUT, "key")
    await install_deferred_failure(postgres_store, f"NEW.dispatch_state = '{dispatch_state}'")
    with pytest.raises(asyncpg.CheckViolationError, match="test commit rejected"):
        await service.dispatch_next("test-deployment")
    assert robot.sent == []
    count = await postgres_store.pool.fetchval("SELECT count(*) FROM ra_commands WHERE dispatch_state = 'DISPATCHING'")
    assert count == 0
    if dispatch_state == "PREPARED":
        assert await postgres_store.pool.fetchval("SELECT count(*) FROM ra_commands") == 0
        assert await postgres_store.pool.fetchval("SELECT task_id FROM ra_execution_guard") is None


async def test_terminal_updates_atomically(persisted_service, postgres_store):
    service, robot = persisted_service
    view = await service.submit(CONTEXT, INPUT, "key")
    await service.dispatch_next("test-deployment")
    command = robot.sent[0]
    event = RobotEvent("result-1", CONTEXT.customer_id, "test-binding", 1, command.command_id, "succeeded")
    await service.on_robot_event(event)
    first = await service.get(CONTEXT, view.task_id)
    await service.on_robot_event(event)
    await service.on_robot_event(RobotEvent("replayed-result", CONTEXT.customer_id, "test-binding", 1,
                                          command.command_id, "succeeded"))
    await service.on_robot_event(RobotEvent("late-running", CONTEXT.customer_id, "test-binding", 1,
                                          command.command_id, "running"))
    assert await service.get(CONTEXT, view.task_id) == first
    assert first.result_summary[0]["outcome"] == "SUCCEEDED"
    async with postgres_store.unit_of_work(CONTEXT) as tx:
        assert (await tx.load_task(view.task_id)).completed == ("s1",)
    assert await postgres_store.pool.fetchval("SELECT next_step_id FROM ra_units") == "s2"
    assert await postgres_store.pool.fetchval("SELECT count(*) FROM ra_robot_events WHERE event_id = 'result-1'") == 1


async def test_failed_terminal_commit_leaves_no_partial_result(persisted_service, postgres_store):
    service, robot = persisted_service
    view = await service.submit(CONTEXT, INPUT, "key")
    await service.dispatch_next("test-deployment")
    before = await service.get(CONTEXT, view.task_id)
    await install_deferred_failure(postgres_store, "NEW.outcome = 'SUCCEEDED'")
    with pytest.raises(asyncpg.CheckViolationError):
        await service.on_robot_event(RobotEvent("failed-commit-event", CONTEXT.customer_id, "test-binding", 1,
                                              robot.sent[0].command_id, "succeeded"))
    assert await service.get(CONTEXT, view.task_id) == before
    assert await postgres_store.pool.fetchval("SELECT count(*) FROM ra_robot_events") == 0
    assert await postgres_store.pool.fetchval("SELECT next_step_id FROM ra_units") == "s1"


async def test_concurrent_dispatch_creates_one_command(persisted_service, postgres_store):
    service, robot = persisted_service
    await service.submit(CONTEXT, INPUT, "key")
    await asyncio.gather(service.dispatch_next("test-deployment"), service.dispatch_next("test-deployment"))
    assert len(robot.sent) == 1
    assert await postgres_store.pool.fetchval("SELECT count(*) FROM ra_commands") == 1
    assert await postgres_store.pool.fetchval("SELECT count(*) FROM ra_execution_guard WHERE task_id IS NOT NULL") == 1


async def test_one_deployment_has_one_active_task(persisted_service, postgres_store):
    service, robot = persisted_service
    first = await service.submit(CONTEXT, INPUT, "first-key")
    second = await service.submit(CONTEXT, INPUT, "second-key")
    await service.dispatch_next("test-deployment")
    await service.dispatch_next("test-deployment")
    assert len(robot.sent) == 1
    assert robot.sent[0].task_id == first.task_id
    assert (await service.get(CONTEXT, second.task_id)).result_summary == ()


async def test_feedback_before_send_return_preserves_success(persisted_service):
    service, robot = persisted_service
    view = await service.submit(CONTEXT, INPUT, "key")
    original_send = robot.send

    async def send_then_receive(command):
        result = await original_send(command)
        await service.on_robot_event(RobotEvent("early", CONTEXT.customer_id, "test-binding", 1,
                                              command.command_id, "succeeded"))
        return result

    robot.send = send_then_receive
    await service.dispatch_next("test-deployment")
    result = await service.get(CONTEXT, view.task_id)
    assert result.result_summary[0]["outcome"] == "SUCCEEDED"
    assert result.result_summary[0]["dispatch_state"] == "SENT"


async def test_event_id_cannot_be_reused_for_different_result(persisted_service, postgres_store):
    service, robot = persisted_service
    view = await service.submit(CONTEXT, INPUT, "key")
    await service.dispatch_next("test-deployment")
    command = robot.sent[0]
    await service.on_robot_event(RobotEvent("same-id", CONTEXT.customer_id, "test-binding", 1,
                                          command.command_id, "running"))
    before = await service.get(CONTEXT, view.task_id)
    with pytest.raises(ConflictError):
        await service.on_robot_event(RobotEvent("same-id", CONTEXT.customer_id, "test-binding", 1,
                                              command.command_id, "succeeded"))
    assert await service.get(CONTEXT, view.task_id) == before
    assert await postgres_store.pool.fetchval("SELECT payload->>'state' FROM ra_robot_events WHERE event_id='same-id'") == "running"


async def test_command_wire_is_immutable_in_database(persisted_service, postgres_store):
    service, robot = persisted_service
    await service.submit(CONTEXT, INPUT, "key")
    await service.dispatch_next("test-deployment")
    with pytest.raises(asyncpg.CheckViolationError):
        await postgres_store.pool.execute("UPDATE ra_commands SET wire_args='{}'::jsonb")
    assert robot.sent[0].wire_args == {"location": "test-table"}


async def test_validation_record_failure_never_creates_executable_plan(persisted_service, postgres_store):
    service, robot = persisted_service
    await postgres_store.pool.execute("""
        CREATE FUNCTION test_fail_validation() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF NEW.kind='validation' THEN RAISE EXCEPTION 'test validation rejected' USING ERRCODE='23514'; END IF;
            RETURN NEW;
        END $$;
        CREATE CONSTRAINT TRIGGER test_validation_commit AFTER INSERT ON ra_decisions
        DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION test_fail_validation();
    """)
    with pytest.raises(asyncpg.CheckViolationError):
        await service.submit(CONTEXT, INPUT, "key")
    await service.dispatch_next("test-deployment")
    assert robot.sent == []
    assert await postgres_store.pool.fetchval("SELECT count(*) FROM ra_plans") == 0
    assert await postgres_store.pool.fetchval("SELECT state FROM ra_tasks") == "PLANNING"
