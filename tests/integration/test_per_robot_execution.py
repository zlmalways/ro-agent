# ruff: noqa: F811
"""M1-M4: real database and transport, controlled simulated model/robot delays."""
import asyncio
from dataclasses import replace

import pytest

from robot_agent.domain.models import Task
from tests.fixtures.submission import submit_and_plan
from tests.integration.test_scheduling import USER, INPUT, fleet  # noqa: F401

pytestmark = pytest.mark.integration


async def test_a_occupied_b_free_both_have_independent_durable_allocations(fleet, postgres_store):
    service, _, _, _, _, frames, connect = fleet
    await connect('A')
    first = await submit_and_plan(service, USER, INPUT, 'first')
    await connect('B')
    second = await submit_and_plan(service, USER, INPUT, 'second')
    assert (first.state, second.state) == ('READY', 'READY')
    assert (first.plan_robot_binding_id, second.plan_robot_binding_id) == ('test-binding', 'binding-b')
    rows = await postgres_store.pool.fetch('SELECT customer_id,task_id,binding_id FROM ra_execution_guard')
    assert {(r['task_id'], r['binding_id']) for r in rows} == {
        (first.task_id, 'test-binding'), (second.task_id, 'binding-b')}
    await service.cancel(USER, second.task_id, second.version, 'stop-b')
    assert (await service.get(USER, first.task_id)).allocation_robot_binding_id == 'test-binding'
    assert (await service.get(USER, second.task_id)).allocation_robot_binding_id is None
    assert not any(frames.values())


async def test_two_new_requests_compete_for_b_in_submission_order(fleet):
    service, _, _, _, _, _, connect = fleet
    await connect('A')
    await submit_and_plan(service, USER, INPUT, 'hold-a')
    await connect('B')
    older = await service.submit(USER, INPUT, 'older')
    newer = await service.submit(USER, INPUT, 'newer')
    await asyncio.gather(service.schedule_pending('test-deployment', task_id=newer.task_id),
                         service.schedule_pending('test-deployment', task_id=older.task_id))
    assert (await service.get(USER, older.task_id)).plan_robot_binding_id == 'binding-b'
    assert (await service.get(USER, newer.task_id)).state == 'QUEUED'


async def test_slow_a_planning_does_not_hold_b_planning_or_dispatch(fleet):
    service, _, _, _, _, frames, connect = fleet
    await connect('A')
    entered, release = asyncio.Event(), asyncio.Event()
    original = service.planner.plan
    async def slow(goal, answers, snapshot):
        if snapshot.robot_binding_id == 'test-binding':
            entered.set()
            await release.wait()
        return await original(goal, answers, snapshot)
    service.planner.plan = slow
    a = await service.submit(USER, INPUT, 'slow-a')
    try:
        await asyncio.wait_for(service.schedule_pending('test-deployment', wait_for_plan=False), 2)
        await asyncio.wait_for(entered.wait(), 2)
        await connect('B')
        b = await submit_and_plan(service, USER, INPUT, 'fast-b')
        assert b.state == 'READY'
        await asyncio.wait_for(service.dispatch_next('test-deployment'), 2)
        assert len(frames['B']) == 1 and not frames['A']
        assert (await service.get(USER, a.task_id)).state == 'PLANNING'
    finally:
        release.set()
        if hasattr(service, 'close_work'):
            await service.close_work()


async def test_slow_a_socket_does_not_hold_b_send_or_new_b_connection(fleet):
    service, robot, _, _, sessions, frames, connect = fleet
    await connect('A')
    await submit_and_plan(service, USER, INPUT, 'a')
    await connect('B')
    b = await submit_and_plan(service, USER, INPUT, 'b')
    entered, release = asyncio.Event(), asyncio.Event()
    async def slow(frame):
        entered.set()
        await release.wait()
        frames['A'].append(frame)
    robot.sessions[(USER.customer_id, 'test-binding')] = replace(sessions['A'], send=slow)
    try:
        await service.dispatch_next('test-deployment', wait_for_send=False)
        await asyncio.wait_for(entered.wait(), 2)
        async def b_sent():
            while not frames['B']:
                await asyncio.sleep(0.01)
        await asyncio.wait_for(b_sent(), 2)
        assert (await service.get(USER, b.task_id)).state == 'RUNNING'
        old_b = sessions['B']
        await asyncio.wait_for(connect('B'), 2)
        await robot.unregister(old_b)
        assert robot.connected_robots(USER.customer_id)['binding-b'] == sessions['B'].generation
    finally:
        release.set()
        if hasattr(service, 'close_work'):
            await service.close_work()


async def test_old_allocation_release_cannot_remove_new_one_even_for_same_task(fleet, postgres_store):
    _, _, resources, _, _, _, connect = fleet
    await connect('A')
    snapshot = await resources.snapshot(USER, 'test-binding')
    task = Task('allocation-task', USER.customer_id, USER.actor_kind, USER.actor_id, 'go', unit_id='allocation-unit')
    async with postgres_store.system_unit_of_work() as tx:
        await tx.insert_task(task)
        old = await tx.claim_slot('test-deployment', task, snapshot)
        await tx.release_allocation(old)
        new = await tx.claim_slot('test-deployment', task, snapshot)
        assert old.allocation_id != new.allocation_id
        await tx.release_allocation(old)
        assert await tx.task_allocation(task) == new


async def test_older_fixed_a_plan_does_not_block_b_or_steal_its_allocation(fleet):
    service, _, _, _, _, frames, connect = fleet
    await connect('A')
    service.policy.result = 'require_confirmation'
    waiting_a = await submit_and_plan(service, USER, INPUT, 'old-a')
    assert waiting_a.state == 'WAITING_APPROVAL'
    service.policy.result = 'allow'
    holding_a = await submit_and_plan(service, USER, INPUT, 'active-a')
    queued = await service.decide(USER, waiting_a.task_id, waiting_a.version, 'approve', 'approve-a')
    assert queued.state == 'QUEUED'
    await connect('B')
    b = await submit_and_plan(service, USER, INPUT, 'new-b')
    assert b.plan_robot_binding_id == 'binding-b'
    assert (await service.get(USER, waiting_a.task_id)).plan_robot_binding_id == 'test-binding'
    # The old fixed-A waiter must not occupy A's dispatch-worker entry either.
    await service.dispatch_next('test-deployment')
    assert len(frames['A']) == len(frames['B']) == 1
    assert (await service.get(USER, holding_a.task_id)).state == 'RUNNING'


async def test_customer_c_does_not_wait_for_customer_a(fleet):
    service, _, _, _, _, frames, connect = fleet
    await connect('A')
    a = await submit_and_plan(service, USER, INPUT, 'a')
    await connect('C')
    customer_c = replace(USER, customer_id='other-customer', actor_id='c-user')
    c = await submit_and_plan(service, customer_c, INPUT, 'c')
    assert c.plan_robot_binding_id == 'binding-c'
    await service.dispatch_next('test-deployment')
    assert len(frames['A']) == len(frames['C']) == 1
    assert (await service.get(USER, a.task_id)).allocation_robot_binding_id == 'test-binding'


@pytest.mark.parametrize('fault', ['cancel', 'disconnect', 'timeout', 'late_conflict'])
async def test_a_fault_does_not_block_b_next_step_or_release_b(fleet, postgres_store, fault):
    service, robot, _, _, sessions, frames, connect = fleet
    await connect('A')
    a = await submit_and_plan(service, USER, INPUT, 'a')
    await connect('B')
    b = await submit_and_plan(service, USER, INPUT, 'b')
    await service.dispatch_next('test-deployment')
    a_command = frames['A'][0]['cmd_id']
    if fault == 'cancel':
        view = await service.get(USER, a.task_id)
        cancelled = await service.cancel(USER, a.task_id, view.version, 'stop-a')
        assert cancelled.state == 'CANCEL_REQUESTED'
        assert frames['A'][-1] == {'type': 'cancel', 'cmd_id': a_command}
        await robot.handle(sessions['A'], dict(type='cmd_status', cmd_id=a_command, state='failed'))
        assert (await service.get(USER, a.task_id)).state == 'CANCELLED'
    elif fault == 'disconnect':
        await robot.unregister(sessions['A'])
        assert (await service.get(USER, a.task_id)).reason_code == 'RESULT_UNKNOWN'
    elif fault == 'timeout':
        service.command_timeout_seconds = 60
        await postgres_store.pool.execute("UPDATE ra_commands SET dispatch_started_at=clock_timestamp()-interval '5 minutes' WHERE task_id=$1", a.task_id)
        await service.expire_commands('test-deployment')
        assert (await service.get(USER, a.task_id)).reason_code == 'RESULT_UNKNOWN'
    else:
        await robot.handle(sessions['A'], dict(type='cmd_status', cmd_id=a_command, state='succeeded'))
        await service.dispatch_next('test-deployment')
        await robot.handle(sessions['A'], dict(type='cmd_status', cmd_id=frames['A'][1]['cmd_id'], state='succeeded'))
        await robot.handle(sessions['A'], dict(type='cmd_status', cmd_id=a_command, state='failed'))
        assert (await service.get(USER, a.task_id)).result_disputed
    a_count = len(frames['A'])
    assert (await service.get(USER, b.task_id)).allocation_robot_binding_id == 'binding-b'
    await robot.handle(sessions['B'], dict(type='cmd_status', cmd_id=frames['B'][0]['cmd_id'], state='succeeded'))
    await asyncio.gather(*(service.dispatch_next('test-deployment') for _ in range(3)))
    assert len(frames['A']) == a_count and len(frames['B']) == 2
    await robot.handle(sessions['B'], dict(type='cmd_status', cmd_id=frames['B'][1]['cmd_id'], state='succeeded'))
    assert (await service.get(USER, b.task_id)).state == 'SUCCEEDED'
    assert not (await postgres_store.pool.fetchrow("SELECT isolation_reason FROM ra_robot_bindings WHERE binding_id='binding-b'"))['isolation_reason']
    if fault in {'disconnect', 'timeout'}:
        assert (await service.get(USER, a.task_id)).allocation_robot_binding_id == 'test-binding'


@pytest.mark.parametrize('restriction', ['disabled', 'map', 'authorization'])
async def test_a_execution_restriction_remains_enforced_without_stopping_b(fleet, postgres_store, restriction):
    service, robot, resources, _, sessions, frames, connect = fleet
    await connect('A')
    a = await submit_and_plan(service, USER, INPUT, 'a')
    await connect('B')
    await submit_and_plan(service, USER, INPUT, 'b')
    if restriction == 'disabled':
        await postgres_store.pool.execute("UPDATE ra_robot_bindings SET enabled=false WHERE binding_id='test-binding'")
    elif restriction == 'map':
        await robot.handle(sessions['A'], dict(type='readiness', waypoint_names=['different']))
    else:
        original = service.policy.evaluate
        async def policy(context, plan, approvals):
            return 'deny' if plan.plan_id == a.plan_id else await original(context, plan, approvals)
        service.policy.evaluate = policy
    await service.dispatch_next('test-deployment')
    result = await service.get(USER, a.task_id)
    assert result.state == ('CANCELLED' if restriction == 'disabled' else 'BLOCKED')
    if restriction == 'disabled':
        assert result.reason_code == 'ROBOT_DISABLED'
    assert not frames['A'] and len(frames['B']) == 1


async def test_background_failure_is_saved_and_not_erased_by_successful_scans(fleet):
    from robot_agent.domain.models import DependencyUnavailable
    from robot_agent.runtime import Runtime
    service, _, _, _, _, frames, connect = fleet
    await connect('A')
    fail, entered = asyncio.Event(), asyncio.Event()
    original = service.planner.plan
    async def planning(goal, answers, snapshot):
        if snapshot.robot_binding_id == 'test-binding':
            entered.set()
            await fail.wait()
            raise DependencyUnavailable('PLANNER_UNAVAILABLE')
        return await original(goal, answers, snapshot)
    service.planner.plan = planning
    runtime = Runtime(owner=None, service=service, deployment_id='test-deployment', scan_interval=0.01)
    await runtime.start()
    a = await service.submit(USER, INPUT, 'a')
    try:
        await asyncio.wait_for(entered.wait(), 2)
        await connect('B')
        b = await service.submit(USER, INPUT, 'b')
        async with asyncio.timeout(3):
            while not frames['B']:
                await asyncio.sleep(0.01)
        fail.set()
        async with asyncio.timeout(3):
            while runtime.error != 'DependencyUnavailable' or not frames['B']:
                await asyncio.sleep(0.01)
        assert (await service.get(USER, a.task_id)).reason_code == 'PLANNER_UNAVAILABLE'
        assert (await service.get(USER, b.task_id)).state == 'RUNNING'
        await asyncio.sleep(0.06)
        assert runtime.error == 'DependencyUnavailable'
        assert not frames['A']
    finally:
        fail.set()
        await runtime.close()


async def test_close_without_execution_enabled_cancels_model_and_frees_unsent_allocation(fleet):
    from robot_agent.runtime import Runtime
    service, _, _, _, _, _, connect = fleet
    await connect('A')
    entered, cancelled = asyncio.Event(), asyncio.Event()
    async def forever(*args):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()
    service.planner.plan = forever
    service.ownership = None
    runtime = Runtime(owner=None, service=service, deployment_id='test-deployment', scan_interval=0.01)
    await runtime.start()
    task = await service.submit(USER, INPUT, 'close')
    await asyncio.wait_for(entered.wait(), 2)
    await asyncio.wait_for(runtime.close(), 2)
    assert cancelled.is_set() and not service._planning and not service._dispatches
    result = await service.get(USER, task.task_id)
    assert result.state == 'BLOCKED' and result.reason_code == 'PLANNING_INTERRUPTED'
    assert result.allocation_robot_binding_id is None


async def test_old_scan_cannot_cancel_newly_started_valid_b_planning(fleet):
    from contextlib import asynccontextmanager
    service, _, _, _, _, _, connect = fleet
    await connect('A')
    await submit_and_plan(service, USER, INPUT, 'a')
    b = await service.submit(USER, INPUT, 'b')
    await service.schedule_pending('test-deployment', task_id=b.task_id)
    assert (await service.get(USER, b.task_id)).state == 'QUEUED'
    await connect('B')
    old_scan, resume_scan, model_entered, finish_model = (asyncio.Event() for _ in range(4))
    original_uow, original_plan = service.store.system_unit_of_work, service.planner.plan
    paused = False
    @asynccontextmanager
    async def pause_after_old_scan():
        nonlocal paused
        scanned = False
        async with original_uow() as tx:
            original_list = tx.list_tasks
            async def listing():
                nonlocal scanned
                tasks = await original_list()
                scanned = True
                return tasks
            tx.list_tasks = listing
            yield tx
        if scanned and not paused:
            paused = True
            old_scan.set()
            await resume_scan.wait()
    async def slow(goal, answers, snapshot):
        model_entered.set()
        await finish_model.wait()
        return await original_plan(goal, answers, snapshot)
    service.store.system_unit_of_work = pause_after_old_scan
    service.planner.plan = slow
    stale = asyncio.create_task(service.schedule_pending('test-deployment', wait_for_plan=False))
    try:
        await asyncio.wait_for(old_scan.wait(), 2)
        await service.schedule_pending('test-deployment', task_id=b.task_id, wait_for_plan=False)
        await asyncio.wait_for(model_entered.wait(), 2)
        resume_scan.set()
        await asyncio.wait_for(stale, 2)
        for _ in range(3):
            await service.schedule_pending('test-deployment', wait_for_plan=False)
        assert (await service.get(USER, b.task_id)).state == 'PLANNING'
        finish_model.set()
        await service.schedule_pending('test-deployment', task_id=b.task_id)
        assert (await service.get(USER, b.task_id)).state == 'READY'
    finally:
        resume_scan.set()
        finish_model.set()
        await stale
        service.store.system_unit_of_work = original_uow


async def test_disconnect_during_a_planning_cancels_only_a_and_b_can_plan(fleet):
    service, robot, _, _, sessions, _, connect = fleet
    await connect('A')
    entered, cancelled = asyncio.Event(), asyncio.Event()
    original = service.planner.plan
    async def slow(goal, answers, snapshot):
        if snapshot.robot_binding_id == 'test-binding':
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        return await original(goal, answers, snapshot)
    service.planner.plan = slow
    a = await service.submit(USER, INPUT, 'a')
    await service.schedule_pending('test-deployment', wait_for_plan=False)
    await asyncio.wait_for(entered.wait(), 2)
    await connect('B')
    await robot.unregister(sessions['A'])
    await asyncio.wait_for(cancelled.wait(), 2)
    a_view = await service.get(USER, a.task_id)
    assert a_view.state == 'BLOCKED' and not a_view.plan_id and a_view.allocation_robot_binding_id is None
    b = await submit_and_plan(service, USER, INPUT, 'b')
    assert b.state == 'READY' and b.plan_robot_binding_id == 'binding-b'


async def test_close_during_socket_send_keeps_unknown_without_retry(fleet):
    service, robot, _, _, sessions, frames, connect = fleet
    await connect('A')
    a = await submit_and_plan(service, USER, INPUT, 'a')
    entered = asyncio.Event()
    async def stalled(frame):
        frames['A'].append(frame)  # The socket may have delivered bytes already.
        entered.set()
        await asyncio.Event().wait()
    robot.sessions[(USER.customer_id, 'test-binding')] = replace(sessions['A'], send=stalled)
    await service.dispatch_next('test-deployment', wait_for_send=False)
    await asyncio.wait_for(entered.wait(), 2)
    await asyncio.wait_for(service.close_work(), 2)
    view = await service.get(USER, a.task_id)
    assert view.reason_code == 'RESULT_UNKNOWN' and view.allocation_robot_binding_id == 'test-binding'
    await service.dispatch_next('test-deployment')
    assert len(frames['A']) == 1 and not service._dispatches
