"""C1 step 20: disabling ends only this robot's unfinished tasks, preserving evidence."""
# ruff: noqa: F811
import asyncio
from dataclasses import replace

import asyncpg
import pytest

from robot_agent.domain.models import ChangeSet, DependencyUnavailable, RobotEvent
from tests.fixtures.submission import submit_and_plan
from tests.integration.test_scheduling import USER, INPUT, fleet  # noqa: F401

pytestmark = pytest.mark.integration


async def disable(store, value=True):
    await store.pool.execute("UPDATE ra_robot_bindings SET enabled=$1,version=version+1 WHERE binding_id='test-binding'", not value)


@pytest.mark.parametrize('state', ['RECEIVED', 'QUEUED', 'PLANNING', 'READY', 'NEEDS_CLARIFICATION',
                                  'WAITING_APPROVAL', 'RUNNING', 'BLOCKED', 'CANCEL_REQUESTED'])
async def test_all_associated_unfinished_states_end(persisted_service, postgres_store, state):
    service, robot = persisted_service
    view = await submit_and_plan(service, USER, INPUT, 'old')
    async with postgres_store.system_unit_of_work() as tx:
        task = await tx.load_task(view.task_id)
        task = replace(task, state=state, reason_code='OLD', resume_phase='PLANNING', version=task.version+1)
        await tx.apply_changes(ChangeSet(task))
        # Plans retain their robot after releasing the execution allocation.
        if state in {'WAITING_APPROVAL', 'NEEDS_CLARIFICATION', 'BLOCKED', 'QUEUED'}:
            await tx.release_allocation(await tx.task_allocation(task))
    await disable(postgres_store)
    await service.schedule_pending('test-deployment')
    ended = await service.get(USER, view.task_id)
    assert (ended.state, ended.reason_code) == ('CANCELLED', 'ROBOT_DISABLED')
    assert ended.allocation_robot_binding_id is None
    async with postgres_store.system_unit_of_work() as tx:
        stored = await tx.load_task(view.task_id)
        assert not stored.resume_phase
        assert (stored.plan, stored.snapshot, stored.goal_text) == (task.plan, task.snapshot, task.goal_text)
    await disable(postgres_store, False)
    await service.schedule_pending('test-deployment')
    await service.dispatch_next('test-deployment')
    assert not robot.sent
    assert await service.submit(USER, INPUT, 'old') == ended


@pytest.mark.parametrize('state', ['SUCCEEDED', 'FAILED', 'CANCELLED'])
async def test_previously_ended_tasks_unchanged(persisted_service, postgres_store, state):
    service, _ = persisted_service
    view = await submit_and_plan(service, USER, INPUT, 'ended')
    async with postgres_store.system_unit_of_work() as tx:
        task = await tx.load_task(view.task_id)
        await tx.apply_changes(ChangeSet(replace(task, state=state, reason_code='ORIGINAL', version=task.version+1)))
        await tx.release_allocation(await tx.task_allocation(task))
    before = await service.get(USER, view.task_id)
    await disable(postgres_store)
    await service.schedule_pending('test-deployment')
    assert await service.get(USER, view.task_id) == before


@pytest.mark.parametrize('entry', ['schedule', 'dispatch', 'disconnect', 'observation', 'feedback'])
async def test_disable_discovered_at_each_entry_retains_unknown_evidence(persisted_service, postgres_store, entry):
    service, robot = persisted_service
    view = await submit_and_plan(service, USER, INPUT, 'old')
    await service.dispatch_next('test-deployment')
    command = robot.sent[0]
    await disable(postgres_store)
    binding = dict(customer_id=USER.customer_id, binding_id='test-binding')
    if entry == 'schedule':
        await service.schedule_pending('test-deployment')
    elif entry == 'dispatch':
        await service.dispatch_next('test-deployment')
    elif entry == 'disconnect':
        await service.on_robot_disconnected(binding, 1)
    elif entry == 'observation':
        await service.on_robot_observation(binding, 1, dict(type='readiness', busy=True))
    else:
        await service.on_robot_event(RobotEvent('old-running', USER.customer_id, 'test-binding', 1, command.command_id, 'running'))
    ended = await service.get(USER, view.task_id)
    assert (ended.state, ended.reason_code) == ('CANCELLED', 'ROBOT_DISABLED')
    assert ended.result_summary[0]['outcome'] == 'RESULT_UNKNOWN'
    assert len(robot.sent) == 1 and ended.allocation_robot_binding_id is None


async def test_b_and_unassigned_tasks_not_cancelled_and_b_continues(fleet, postgres_store):
    service, robot, _, _, sessions, frames, connect = fleet
    await connect('A')
    a = await submit_and_plan(service, USER, INPUT, 'a')
    await connect('B')
    b = await submit_and_plan(service, USER, INPUT, 'b')
    unassigned = await submit_and_plan(service, USER, INPUT, 'waiting')
    assert unassigned.state == 'QUEUED' and unassigned.plan_robot_binding_id is None
    await service.dispatch_next('test-deployment')
    await disable(postgres_store)
    await service.schedule_pending('test-deployment')
    assert (await service.get(USER, a.task_id)).reason_code == 'ROBOT_DISABLED'
    assert (await service.get(USER, b.task_id)).state == 'RUNNING'
    assert (await service.get(USER, unassigned.task_id)).state == 'QUEUED'
    for index in range(2):
        command = frames['B'][index]
        await robot.handle(sessions['B'], dict(type='cmd_status', cmd_id=command['cmd_id'], state='succeeded'))
        await service.dispatch_next('test-deployment')
    assert (await service.get(USER, b.task_id)).state == 'SUCCEEDED'
    await service.schedule_pending('test-deployment')
    await service.dispatch_next('test-deployment')
    assert (await service.get(USER, unassigned.task_id)).plan_robot_binding_id == 'binding-b'
    assert len(frames['A']) == 1 and len(frames['B']) == 3


@pytest.mark.parametrize('receipt', ['succeeded', 'failed', 'running', 'conflict'])
async def test_old_feedback_only_updates_history_after_reenable(persisted_service, postgres_store, receipt):
    service, robot = persisted_service
    old = await submit_and_plan(service, USER, INPUT, 'old')
    await service.dispatch_next('test-deployment')
    command = robot.sent[0]
    await disable(postgres_store)
    await service.schedule_pending('test-deployment')
    await disable(postgres_store, False)
    new = await submit_and_plan(service, USER, INPUT, 'new')
    await service.dispatch_next('test-deployment')
    async def report(key, state):
        await service.on_robot_event(RobotEvent(key, USER.customer_id, 'test-binding', 1, command.command_id, state))
    if receipt == 'conflict':
        await report('success', 'succeeded')
        await report('conflict', 'failed')
    else:
        await report('late', receipt)
        await report('late', receipt)
    ended = await service.get(USER, old.task_id)
    assert (ended.state, ended.reason_code) == ('CANCELLED', 'ROBOT_DISABLED')
    assert (await service.get(USER, new.task_id)).allocation_robot_binding_id == 'test-binding'
    assert (await service.get(USER, new.task_id)).state == 'RUNNING'
    assert not await postgres_store.pool.fetchval('SELECT isolation_reason FROM ra_robot_bindings')
    async with postgres_store.system_unit_of_work() as tx:
        assert (await tx.load_task(old.task_id)).completed == ()
    await service.dispatch_next('test-deployment')
    assert len(robot.sent) == 2


@pytest.mark.parametrize('scan', [False, True])
@pytest.mark.parametrize('outcome', ['plan', 'error'])
async def test_disable_while_model_returns_cannot_revive_task(persisted_service, postgres_store, scan, outcome):
    service, robot = persisted_service
    original = service.planner.plan
    entered, release = asyncio.Event(), asyncio.Event()
    async def delayed(*args):
        entered.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            pass  # Deliberately model a provider result arriving after cancellation.
        if outcome == 'error':
            raise DependencyUnavailable('PLANNER_UNAVAILABLE')
        return await original(*args)
    service.planner.plan = delayed
    view = await service.submit(USER, INPUT, 'old')
    pending = asyncio.create_task(service.schedule_pending('test-deployment', task_id=view.task_id))
    try:
        await asyncio.wait_for(entered.wait(), 3)
        await disable(postgres_store)
        if scan:
            await service.schedule_pending('test-deployment', wait_for_plan=False)
        release.set()
        await asyncio.wait_for(pending, 3)
        ended = await service.get(USER, view.task_id)
        assert (ended.state, ended.reason_code) == ('CANCELLED', 'ROBOT_DISABLED')
        assert ended.plan_id is None and ended.allocation_robot_binding_id is None
        assert not robot.sent
    finally:
        release.set()
        await asyncio.gather(pending, return_exceptions=True)
        await service.close_work()


async def test_disable_during_socket_write_preserves_unknown_and_no_next_step(persisted_service, postgres_store):
    service, robot = persisted_service
    old = await submit_and_plan(service, USER, INPUT, 'old')
    entered, release = asyncio.Event(), asyncio.Event()
    original = robot.send
    async def delayed(command):
        entered.set()
        await release.wait()
        return await original(command)
    robot.send = delayed
    pending = asyncio.create_task(service.dispatch_next('test-deployment'))
    try:
        await asyncio.wait_for(entered.wait(), 3)
        await disable(postgres_store)
        await service.schedule_pending('test-deployment')
        release.set()
        await asyncio.wait_for(pending, 3)
        ended = await service.get(USER, old.task_id)
        assert (ended.state, ended.reason_code) == ('CANCELLED', 'ROBOT_DISABLED')
        assert ended.result_summary[0]['outcome'] == 'RESULT_UNKNOWN'
        assert ended.result_summary[0]['dispatch_state'] == 'SENT'
        await service.dispatch_next('test-deployment')
        assert len(robot.sent) == 1
    finally:
        release.set()
        await asyncio.gather(pending, return_exceptions=True)


async def test_disable_commit_failure_rolls_back_task_and_allocation(persisted_service, postgres_store):
    service, _ = persisted_service
    view = await submit_and_plan(service, USER, INPUT, 'old')
    await disable(postgres_store)
    await postgres_store.pool.execute("""
        CREATE FUNCTION reject_disable() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN IF NEW.reason_code='ROBOT_DISABLED' THEN
          RAISE EXCEPTION 'disable commit rejected' USING ERRCODE='23514'; END IF; RETURN NEW; END $$;
        CREATE CONSTRAINT TRIGGER disable_commit AFTER UPDATE ON ra_tasks DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION reject_disable();
    """)
    with pytest.raises(asyncpg.CheckViolationError, match='disable commit rejected'):
        await service.schedule_pending('test-deployment')
    assert await service.get(USER, view.task_id) == view
    assert await postgres_store.pool.fetchval('SELECT task_id FROM ra_execution_guard') == view.task_id
