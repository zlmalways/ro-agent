"""C1 step 17: restart ends tasks, but preserves command evidence."""
import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace

import asyncpg
import pytest

from robot_agent.domain.models import ChangeSet, RobotEvent, TaskInput, TrustedContext
from robot_agent.runtime import Runtime
from tests.fixtures.submission import submit_and_plan

pytestmark = pytest.mark.integration
USER = TrustedContext("test-customer", "user", "test-user")
INPUT = TaskInput("restart", "web", "先去桌边，再拿杯子", "")


@pytest.mark.parametrize("state", ["RECEIVED", "QUEUED", "PLANNING", "READY", "NEEDS_CLARIFICATION",
                                   "WAITING_APPROVAL", "RUNNING", "BLOCKED", "CANCEL_REQUESTED"])
async def test_restart_ends_every_unfinished_state(persisted_service, postgres_store, state):
    service, robot = persisted_service
    view = await submit_and_plan(service, USER, INPUT, "old")
    async with postgres_store.system_unit_of_work() as tx:
        old = await tx.load_task(view.task_id)
        old = replace(old, state=state, reason_code="OLD_REASON", resume_phase="PLANNING", version=old.version + 1)
        await tx.apply_changes(ChangeSet(old))
    await service.recover("test-deployment")
    async with postgres_store.system_unit_of_work() as tx:
        ended = await tx.load_task(view.task_id)
        assert ended.state == "CANCELLED" and ended.reason_code == "RESTART_TASK_LOST"
        assert not ended.resume_phase
        assert (ended.plan, ended.snapshot, ended.goal_text, ended.answers) == (old.plan, old.snapshot, old.goal_text, old.answers)
        assert await tx.task_allocation(ended) is None
    await service.schedule_pending("test-deployment")
    await service.dispatch_next("test-deployment")
    assert not robot.sent
    # Original request replay is historical lookup, never a restart retry.
    again = await service.submit(USER, INPUT, "old")
    assert again.task_id == view.task_id and again.state == "CANCELLED"
    await service.recover("test-deployment")
    assert await service.get(USER, view.task_id) == again


@pytest.mark.parametrize("state", ["SUCCEEDED", "FAILED", "CANCELLED"])
async def test_restart_preserves_already_ended_task(persisted_service, postgres_store, state):
    service, _ = persisted_service
    view = await submit_and_plan(service, USER, INPUT, "ended")
    async with postgres_store.system_unit_of_work() as tx:
        task = await tx.load_task(view.task_id)
        task = replace(task, state=state, reason_code="ORIGINAL", version=task.version + 1)
        await tx.apply_changes(ChangeSet(task))
        await tx.release_allocation(await tx.task_allocation(task))
    before = await service.get(USER, view.task_id)
    await service.recover("test-deployment")
    assert await service.get(USER, view.task_id) == before


@pytest.mark.parametrize("receipt", ["succeeded", "failed", "running", "conflict"])
async def test_old_receipt_only_updates_history_and_never_releases_new_task(persisted_service, postgres_store, receipt):
    service, robot = persisted_service
    old = await submit_and_plan(service, USER, INPUT, "old")
    await service.dispatch_next("test-deployment")
    command = robot.sent[0]
    await service.recover("test-deployment")
    ended = await service.get(USER, old.task_id)
    assert ended.state == "CANCELLED" and ended.result_summary[0]["outcome"] == "RESULT_UNKNOWN"
    new = await submit_and_plan(service, USER, INPUT, "new")
    assert new.allocation_robot_binding_id == "test-binding"
    await service.dispatch_next("test-deployment")
    assert len(robot.sent) == 2
    async def feedback(key, state):
        await service.on_robot_event(RobotEvent(key, USER.customer_id, "test-binding", 1, command.command_id, state))
    if receipt == "conflict":
        await feedback("late-success", "succeeded")
        await feedback("late-conflict", "failed")
    else:
        await feedback("late", receipt)
        await feedback("late", receipt)
    current = await service.get(USER, old.task_id)
    assert current.state == "CANCELLED" and current.reason_code == "RESTART_TASK_LOST"
    assert (await service.get(USER, new.task_id)).allocation_robot_binding_id == "test-binding"
    assert (await service.get(USER, new.task_id)).state == "RUNNING"
    assert not await postgres_store.pool.fetchval("SELECT isolation_reason FROM ra_robot_bindings")
    async with postgres_store.system_unit_of_work() as tx:
        assert (await tx.load_task(old.task_id)).completed == ()
    await service.on_robot_disconnected({"customer_id": USER.customer_id, "binding_id": "test-binding"}, 1)
    assert (await service.get(USER, old.task_id)).reason_code == "RESTART_TASK_LOST"
    await service.dispatch_next("test-deployment")
    assert len(robot.sent) == 2


async def test_start_without_execution_also_ends_old_tasks(persisted_service):
    service, robot = persisted_service
    old = await service.submit(USER, INPUT, "old")
    service.ownership = None
    runtime = Runtime(owner=None, service=service, deployment_id="test-deployment", scan_interval=0.01)
    await runtime.start()
    try:
        assert (await service.get(USER, old.task_id)).reason_code == "RESTART_TASK_LOST"
        assert not robot.sent
    finally:
        await runtime.close()


async def test_restart_commit_failure_rolls_back_and_runtime_never_starts(persisted_service, postgres_store):
    service, _ = persisted_service
    a = await submit_and_plan(service, USER, INPUT, "a")
    b = await service.submit(USER, INPUT, "b")
    before = [await service.get(USER, task.task_id) for task in (a, b)]
    await postgres_store.pool.execute("""
        CREATE FUNCTION reject_restart() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN IF NEW.reason_code='RESTART_TASK_LOST' THEN
          RAISE EXCEPTION 'restart commit rejected' USING ERRCODE='23514'; END IF; RETURN NEW; END $$;
        CREATE CONSTRAINT TRIGGER restart_commit AFTER UPDATE ON ra_tasks DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION reject_restart();
    """)
    service.ownership = None
    runtime = Runtime(owner=None, service=service, deployment_id="test-deployment", scan_interval=0.01)
    try:
        with pytest.raises(asyncpg.CheckViolationError, match="restart commit rejected"):
            await runtime.start()
        assert runtime.loop_task is None
        assert [await service.get(USER, task.task_id) for task in (a, b)] == before
        assert await postgres_store.pool.fetchval("SELECT task_id FROM ra_execution_guard") == a.task_id
    finally:
        await runtime.close()


@pytest.mark.parametrize("outcome", ["plan", "clarify", "unsupported", "invalid", "deny", "provider_error"])
async def test_planning_commit_cleanup_does_not_swallow_results(persisted_service, postgres_store, outcome):
    from robot_agent.domain.models import CandidatePlan, DependencyUnavailable
    service, _ = persisted_service
    original_plan = service.planner.plan
    async def planner(*args):
        if outcome == "provider_error":
            raise DependencyUnavailable("PLANNER_UNAVAILABLE")
        if outcome == "clarify":
            return CandidatePlan.from_dict({"outcome": "clarify", "question": "去哪里？", "required_slots": ["location"]})
        if outcome == "unsupported":
            return CandidatePlan.from_dict({"outcome": "unsupported", "reason": "unavailable"})
        value = await original_plan(*args)
        return replace(value, steps=()) if outcome == "invalid" else value
    service.planner.plan = planner
    if outcome == "deny":
        service.policy.result = "deny"
    view = await service.submit(USER, INPUT, "race")
    original_uow = postgres_store.system_unit_of_work
    committed, release = asyncio.Event(), asyncio.Event()
    @asynccontextmanager
    async def delayed_return():
        planning = asyncio.current_task().get_name().startswith("plan:")
        async with original_uow() as tx:
            yield tx
        if planning and not committed.is_set():
            async with original_uow() as tx:
                task = await tx.load_task(view.task_id)
            if task.state != "PLANNING":
                committed.set()
                await release.wait()
    postgres_store.system_unit_of_work = delayed_return
    pending = asyncio.create_task(service.schedule_pending("test-deployment", task_id=view.task_id))
    try:
        await asyncio.wait_for(committed.wait(), 3)
        await service._cancel_invalid_planning()
        assert not pending.done()
        release.set()
        if outcome == "provider_error":
            with pytest.raises(DependencyUnavailable, match="PLANNER_UNAVAILABLE"):
                await asyncio.wait_for(pending, 3)
        else:
            await asyncio.wait_for(pending, 3)
        result = await service.get(USER, view.task_id)
        expected = {"plan": "READY", "clarify": "NEEDS_CLARIFICATION", "unsupported": "FAILED",
                    "invalid": "FAILED", "deny": "FAILED", "provider_error": "BLOCKED"}
        assert result.state == expected[outcome]
        assert result.reason_code != "PLANNING_INTERRUPTED"
    finally:
        release.set()
        await asyncio.gather(pending, return_exceptions=True)
        postgres_store.system_unit_of_work = original_uow


@pytest.mark.parametrize("operation", ["user_cancel", "shutdown"])
async def test_protected_planning_return_still_allows_cancel_and_shutdown(persisted_service, postgres_store, operation):
    service, robot = persisted_service
    view = await service.submit(USER, INPUT, "cancel-during-return")
    committed, release = asyncio.Event(), asyncio.Event()
    original = postgres_store.system_unit_of_work
    @asynccontextmanager
    async def delayed():
        planning = asyncio.current_task().get_name().startswith("plan:")
        async with original() as tx:
            yield tx
        if planning and not committed.is_set():
            async with original() as tx:
                task = await tx.load_task(view.task_id)
            if task.state == "READY":
                committed.set()
                await release.wait()
    postgres_store.system_unit_of_work = delayed
    pending = asyncio.create_task(service.schedule_pending("test-deployment", task_id=view.task_id))
    try:
        await asyncio.wait_for(committed.wait(), 3)
        if operation == "user_cancel":
            ready = await service.get(USER, view.task_id)
            cancelled = await asyncio.wait_for(service.cancel(USER, view.task_id, ready.version, "cancel"), 3)
            assert cancelled.state == "CANCELLED" and cancelled.allocation_robot_binding_id is None
        else:
            await asyncio.wait_for(service.close_work(), 3)
            assert not service._planning
        release.set()
        await asyncio.wait_for(pending, 3)
        await service.dispatch_next("test-deployment")
        assert not robot.sent
    finally:
        release.set()
        await asyncio.gather(pending, return_exceptions=True)
        postgres_store.system_unit_of_work = original
