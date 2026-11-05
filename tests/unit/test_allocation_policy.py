"""Selection + allocation is one locked operation; no executor-side acquisition."""
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from robot_agent.application.scheduling import schedule
from robot_agent.domain.models import Task, RobotAllocation, TrustedContext
from tests.fixtures.minimal import TestResources as ResourcesFixture


@pytest.fixture
async def data():
    task = Task('task', 'test-customer', 'user', 'test-user', '去桌边', unit_id='unit')
    snapshot = await ResourcesFixture().snapshot(TrustedContext('test-customer', 'user', 'test-user'))
    tx = SimpleNamespace(task_allocation=AsyncMock(return_value=None), allocations=AsyncMock(return_value={}), prior_waiters=AsyncMock(return_value=[]),
                         claim_slot=AsyncMock(return_value=RobotAllocation(task.customer_id, task.task_id,
                                                                         snapshot.robot_binding_id, snapshot)))
    return task, snapshot, tx


@pytest.mark.parametrize('busy,reason,status', [(True, 'ROBOT_BUSY', 'waiting'), (None, 'ROBOT_STATE_UNKNOWN', 'blocked')])
async def test_busy_or_unknown_does_not_acquire(data, busy, reason, status):
    task, snapshot, tx = data
    result = await schedule(task, [replace(snapshot, readiness={'busy':busy})], tx, 'test-deployment')
    assert (result.status, result.reason_code) == (status, reason)
    tx.claim_slot.assert_not_called()


async def test_existing_allocation_is_reused_and_busy_does_not_drop_it(data):
    task, snapshot, tx = data
    acquired = await schedule(task, [snapshot], tx, 'test-deployment')
    tx.task_allocation.return_value = acquired.allocation
    tx.allocations.return_value = {snapshot.robot_binding_id: acquired.allocation}
    repeated = await schedule(task, [snapshot], tx, 'test-deployment')
    busy = await schedule(task, [replace(snapshot, readiness={'busy':True})], tx, 'test-deployment')
    assert repeated == acquired and busy.allocation == acquired.allocation
    assert busy.status == 'waiting'
    tx.claim_slot.assert_awaited_once()


async def test_prior_waiter_or_other_allocation_cannot_be_overtaken(data):
    task, snapshot, tx = data
    tx.prior_waiters.return_value = [replace(task, task_id="prior")]
    assert (await schedule(task, [snapshot], tx, 'test-deployment')).reason_code == 'QUEUE_ORDER'
    tx.allocations.return_value = {snapshot.robot_binding_id: RobotAllocation(task.customer_id, 'other-task', snapshot.robot_binding_id, snapshot)}
    assert (await schedule(task, [snapshot], tx, 'test-deployment')).reason_code == 'EXECUTION_CAPACITY_BUSY'
    tx.claim_slot.assert_not_called()


async def test_foreign_customer_or_changed_connection_cannot_reuse_allocation(data):
    task, snapshot, tx = data
    assert (await schedule(task, [replace(snapshot, customer_id='other')], tx, 'test-deployment')).status == 'blocked'
    tx.task_allocation.return_value = RobotAllocation(task.customer_id, task.task_id, snapshot.robot_binding_id, snapshot)
    tx.allocations.return_value = {snapshot.robot_binding_id: tx.task_allocation.return_value}
    assert (await schedule(task, [replace(snapshot, generation=2)], tx, 'test-deployment')).reason_code == 'CONNECTION_CHANGED'
    tx.claim_slot.assert_not_called()


@pytest.mark.parametrize('failure', [False, RuntimeError('storage failed')])
async def test_failed_allocation_is_error_not_normal_queue(data, failure):
    task, snapshot, tx = data
    if isinstance(failure, Exception):
        tx.claim_slot.side_effect = failure
    else:
        tx.claim_slot.return_value = failure
    with pytest.raises(RuntimeError):
        await schedule(task, [snapshot], tx, 'test-deployment')


@pytest.mark.parametrize('states,reason,status,chosen', [
    ([], 'NO_ONLINE_ROBOT', 'blocked', None),
    ([True, True], 'ROBOT_BUSY', 'waiting', None),
    ([True, None], 'ROBOT_STATE_UNKNOWN', 'blocked', None),
    ([None, False], '', 'assigned', 'b-1'),
    ([True, False], '', 'assigned', 'b-1'),
    ([False, False], 'MULTI_ROBOT_SCHEDULING_NOT_IMPLEMENTED', 'blocked', None),
    ([True, True, None, False, True], '', 'assigned', 'b-3'),
    ([True, True, None, False, False], 'MULTI_ROBOT_SCHEDULING_NOT_IMPLEMENTED', 'blocked', None),
])
async def test_filters_busy_and_unknown_before_counting_any_size_fleet(data, states, reason, status, chosen):
    task, snapshot, tx = data
    candidates = [replace(snapshot, robot_binding_id=f'b-{i}', readiness={'busy': state}) for i, state in enumerate(states)]
    if chosen:
        selected = next(s for s in candidates if s.robot_binding_id == chosen)
        tx.claim_slot.return_value = RobotAllocation(task.customer_id, task.task_id, chosen, selected)
    result = await schedule(task, candidates, tx, 'test-deployment')
    assert (result.status, result.reason_code) == (status, reason)
    assert (result.allocation.robot_binding_id if result.allocation else None) == chosen
    if not chosen:
        tx.claim_slot.assert_not_called()


async def test_ambiguous_old_request_does_not_block_a_fixed_plan(data):
    from robot_agent.domain.models import Plan
    task, a, tx = data
    b = replace(a, robot_binding_id='b')
    fixed = replace(task, plan=Plan('plan', 1, (), b.snapshot_id), snapshot=b)
    tx.prior_waiters.return_value = [replace(task, task_id='older')]
    tx.claim_slot.return_value = RobotAllocation(task.customer_id, task.task_id, 'b', b)
    result = await schedule(fixed, [a, b], tx, 'test-deployment')
    assert result.status == 'assigned' and result.allocation.robot_binding_id == 'b'
