# ruff: noqa: F811
"""Stage 0A-0E: allocation before planning, real PostgreSQL transaction boundaries."""
from tests.fixtures.submission import submit_and_plan
import asyncio
from dataclasses import replace
from unittest.mock import AsyncMock

import asyncpg
import pytest

from robot_agent.domain.models import CandidatePlan, DependencyUnavailable
from tests.integration.test_scheduling import USER, INPUT, fleet  # noqa: F401


pytestmark = pytest.mark.integration


async def slot(store):
    async with store.system_unit_of_work() as tx:
        return (await tx.allocations(USER.customer_id)).get("test-binding")


async def test_two_requests_only_first_plans_second_does_not_wait_for_it(fleet, postgres_store):
    service, _, _, _, _, _, connect = fleet
    await connect('A')
    entered, resume = asyncio.Event(), asyncio.Event()
    original = service.planner.plan
    async def slow(*args):
        entered.set()
        await resume.wait()
        return await original(*args)
    service.planner.plan = AsyncMock(side_effect=slow)
    first_request = asyncio.create_task(submit_and_plan(service, USER, INPUT, 'first'))
    try:
        await asyncio.wait_for(entered.wait(), 3)
        first_allocation = await slot(postgres_store)
        assert first_allocation and await postgres_store.pool.fetchval('SELECT count(*) FROM ra_plans') == 0
        peer = replace(USER, actor_id='peer')
        second = await asyncio.wait_for(submit_and_plan(service, peer, INPUT, 'second'), 1)
        assert second.state == 'QUEUED' and second.plan_id is None
        assert service.planner.plan.await_count == 1
        assert (await slot(postgres_store)).task_id == first_allocation.task_id
        assert await postgres_store.pool.fetchval('SELECT count(*) FROM ra_commands') == 0
    finally:
        resume.set()
        await first_request


async def test_second_request_does_not_receive_first_requests_provider_error(fleet):
    service, _, _, _, _, _, connect = fleet
    await connect('A')
    entered, resume = asyncio.Event(), asyncio.Event()
    async def fail(*args):
        entered.set()
        await resume.wait()
        raise DependencyUnavailable('PLANNER_UNAVAILABLE')
    service.planner.plan = AsyncMock(side_effect=fail)
    first = asyncio.create_task(submit_and_plan(service, USER, INPUT, 'first'))
    try:
        await asyncio.wait_for(entered.wait(), 3)
        second = await asyncio.wait_for(submit_and_plan(service, replace(USER, actor_id='peer'), INPUT, 'second'), 1)
        assert second.state == 'QUEUED'
    finally:
        resume.set()
        with pytest.raises(DependencyUnavailable, match='PLANNER_UNAVAILABLE'):
            await first


async def test_cancel_during_planning_releases_slot_and_late_response_cannot_revive(fleet, postgres_store):
    service, _, _, _, _, frames, connect = fleet
    await connect('A')
    entered, resume = asyncio.Event(), asyncio.Event()
    original = service.planner.plan
    async def slow(*args):
        entered.set()
        await resume.wait()
        return await original(*args)
    service.planner.plan = slow
    first = asyncio.create_task(submit_and_plan(service, USER, INPUT, 'slow'))
    try:
        await asyncio.wait_for(entered.wait(), 3)
        allocation = await slot(postgres_store)
        view = await service.get(USER, allocation.task_id)
        cancelled = await service.cancel(USER, view.task_id, view.version, 'cancel')
        assert cancelled.state == 'CANCELLED' and await slot(postgres_store) is None
        service.planner.plan = original
        next_task = await submit_and_plan(service, USER, INPUT, 'next')
        assert next_task.state == 'READY'
        resume.set()
        assert (await first).state == 'CANCELLED'
        assert (await slot(postgres_store)).task_id == next_task.task_id
        assert not any(frames.values())
    finally:
        resume.set()
        if not first.done():
            await first


@pytest.mark.parametrize('outcome', ['clarify', 'unsupported', 'invalid', 'approval', 'provider'])
async def test_preexecution_wait_or_failure_frees_allocation(fleet, postgres_store, outcome):
    service, _, _, _, _, frames, connect = fleet
    await connect('A')
    if outcome == 'provider':
        service.planner.plan = AsyncMock(side_effect=DependencyUnavailable('PLANNER_UNAVAILABLE'))
        with pytest.raises(DependencyUnavailable):
            await submit_and_plan(service, USER, INPUT, outcome)
    else:
        if outcome == 'approval':
            service.policy.result = 'require_confirmation'
        elif outcome == 'invalid':
            service.planner.value = dict(outcome='plan', steps=[dict(step_id='s1', action='move_to',
                                         args={'location':'invented'}, depends_on=[])])
        else:
            service.planner.plan = AsyncMock(return_value=CandidatePlan(outcome, question='去哪里？'))
        result = await submit_and_plan(service, USER, INPUT, outcome)
        assert result.state in {'FAILED', 'NEEDS_CLARIFICATION', 'WAITING_APPROVAL'}
    assert await slot(postgres_store) is None
    assert not any(frames.values())


async def test_two_waiting_requests_cannot_both_acquire_or_plan(fleet, postgres_store):
    service, robot, _, _, sessions, frames, connect = fleet
    await connect('A')
    service.planner.plan = AsyncMock(wraps=service.planner.plan)
    first = await submit_and_plan(service, USER, INPUT, 'first')
    second = await submit_and_plan(service, USER, INPUT, 'second')
    assert second.state == 'QUEUED' and not second.plan_steps
    for index in (0, 1):
        await service.dispatch_next('test-deployment')
        await robot.handle(sessions['A'], dict(type='cmd_status', cmd_id=frames['A'][index]['cmd_id'], state='succeeded'))
    assert (await service.get(USER, first.task_id)).state == 'SUCCEEDED'
    await asyncio.gather(service.schedule_pending('test-deployment'), service.schedule_pending('test-deployment'))
    assert service.planner.plan.await_count == 2
    assert (await slot(postgres_store)).task_id == second.task_id
    await service.dispatch_next('test-deployment')
    assert len(frames['A']) == 3


async def test_unsent_preflight_failure_releases_but_unknown_send_retains(fleet, postgres_store):
    service, robot, _, _, sessions, frames, connect = fleet
    await connect('A')
    first = await submit_and_plan(service, USER, INPUT, 'first')
    await robot.handle(sessions['A'], dict(type='readiness', nav_server_ready=False))
    await service.dispatch_next('test-deployment')
    assert (await service.get(USER, first.task_id)).state == 'BLOCKED'
    assert await slot(postgres_store) is None and not frames['A']
    await robot.handle(sessions['A'], dict(type='readiness', nav_server_ready=True))
    second = await submit_and_plan(service, USER, INPUT, 'second')
    await service.dispatch_next('test-deployment')
    await robot.unregister(sessions['A'])
    assert (await service.get(USER, second.task_id)).reason_code == 'RESULT_UNKNOWN'
    assert (await slot(postgres_store)).task_id == second.task_id


async def test_slot_write_failure_rolls_back_and_is_not_reported_as_queue(fleet, postgres_store):
    service, _, _, _, _, _, connect = fleet
    await connect('A')
    service.planner.plan = AsyncMock(side_effect=AssertionError('must not plan'))
    await postgres_store.pool.execute("""CREATE FUNCTION reject_allocation() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN IF NEW.task_id IS NOT NULL THEN RAISE EXCEPTION 'allocation failed' USING ERRCODE='23514'; END IF;
        RETURN NEW; END $$;
        CREATE CONSTRAINT TRIGGER reject_allocation AFTER INSERT OR UPDATE ON ra_execution_guard
        DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION reject_allocation();""")
    with pytest.raises(asyncpg.CheckViolationError, match='allocation failed'):
        await submit_and_plan(service, USER, INPUT, 'fail')
    assert await slot(postgres_store) is None
    assert await postgres_store.pool.fetchval("SELECT state FROM ra_tasks") == 'RECEIVED'
    service.planner.plan.assert_not_called()


async def test_restart_ends_interrupted_planning_and_ignores_old_model(fleet, postgres_store):
    service, robot, _, _, sessions, frames, connect = fleet
    await connect('A')
    original = service.planner.plan
    entered, resume = asyncio.Event(), asyncio.Event()
    async def slow(*args):
        entered.set()
        await resume.wait()
        return await original(*args)
    service.planner.plan = AsyncMock(side_effect=slow)
    request = asyncio.create_task(submit_and_plan(service, USER, INPUT, 'interrupted'))
    try:
        await asyncio.wait_for(entered.wait(), 3)
        allocation = await slot(postgres_store)
        await robot.unregister(sessions['A'])
        await service.recover('test-deployment')
        await service.schedule_pending('test-deployment')
        queued = await service.get(USER, allocation.task_id)
        assert queued.state == 'CANCELLED' and queued.plan_id is None
        assert await slot(postgres_store) is None and service.planner.plan.await_count == 1
        resume.set()
        assert (await request).state == 'CANCELLED'
        service.planner.plan = AsyncMock(wraps=original)
        await connect('A')
        await service.schedule_pending('test-deployment')
        assert (await service.get(USER, allocation.task_id)).state == 'CANCELLED'
        assert await slot(postgres_store) is None
        service.planner.plan.assert_not_awaited()
        assert not any(frames.values())
    finally:
        resume.set()
        await request


async def test_binding_disabled_after_dispatch_check_releases_unsent_allocation(fleet, postgres_store):
    service, _, _, _, _, frames, connect = fleet
    await connect('A')
    task = await submit_and_plan(service, USER, INPUT, 'disable-before-prepare')
    check = service.preflight.check
    async def disable_after_check(*args, **kwargs):
        result = await check(*args, **kwargs)
        await postgres_store.pool.execute("UPDATE ra_robot_bindings SET enabled=false WHERE binding_id='test-binding'")
        return result
    service.preflight.check = disable_after_check
    await service.dispatch_next('test-deployment')
    result = await service.get(USER, task.task_id)
    assert result.state == 'CANCELLED' and result.reason_code == 'ROBOT_DISABLED'
    assert await slot(postgres_store) is None
    assert not any(frames.values())


async def test_check_failure_after_prepared_commit_releases_allocation(fleet, postgres_store):
    from robot_agent.domain.models import PreflightBlocked
    service, _, _, _, _, frames, connect = fleet
    await connect('A')
    task = await submit_and_plan(service, USER, INPUT, 'second-check')
    check, count = service.preflight.check, 0
    async def fails_second(*args, **kwargs):
        nonlocal count
        count += 1
        if count == 2:
            raise PreflightBlocked('READINESS_UNKNOWN')
        return await check(*args, **kwargs)
    service.preflight.check = fails_second
    await service.dispatch_next('test-deployment')
    result = await service.get(USER, task.task_id)
    assert result.state == 'BLOCKED' and result.reason_code == 'READINESS_UNKNOWN'
    assert result.result_summary[0]['dispatch_state'] == 'PREPARED'
    assert await slot(postgres_store) is None and not any(frames.values())


async def test_cancel_during_dispatch_check_cannot_be_overwritten_by_binding_change(fleet, postgres_store):
    service, _, _, _, _, frames, connect = fleet
    await connect('A')
    task = await submit_and_plan(service, USER, INPUT, 'cancel-during-check')
    check = service.preflight.check
    async def cancel_then_disable(*args, **kwargs):
        result = await check(*args, **kwargs)
        await service.cancel(USER, task.task_id, task.version, 'cancel')
        await postgres_store.pool.execute("UPDATE ra_robot_bindings SET enabled=false WHERE binding_id='test-binding'")
        return result
    service.preflight.check = cancel_then_disable
    await service.dispatch_next('test-deployment')
    assert (await service.get(USER, task.task_id)).state == 'CANCELLED'
    assert await slot(postgres_store) is None and not any(frames.values())
