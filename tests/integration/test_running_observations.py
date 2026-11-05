"""Ordinary reports update evidence; only a new dispatch needs preflight checks."""
# ruff: noqa: F811
import asyncio
from dataclasses import asdict
import json

import pytest

from tests.fixtures.submission import submit_and_plan
from tests.integration.test_scheduling import USER, INPUT, fleet  # noqa: F401

pytestmark = pytest.mark.integration


async def start(fleet, receipt='running'):
    service, robot, _, _, sessions, frames, connect = fleet
    await connect('A')
    task = await submit_and_plan(service, USER, INPUT, 'ordinary-report')
    await service.dispatch_next('test-deployment')
    command = frames['A'][0]
    if receipt:
        await robot.handle(sessions['A'], dict(type='cmd_status', cmd_id=command['cmd_id'], state=receipt))
    assert (await service.get(USER, task.task_id)).state == 'RUNNING'
    return task, command


@pytest.mark.parametrize('kind', ['readiness', 'telemetry'])
@pytest.mark.parametrize('receipt', [None, 'running'])
async def test_identical_periodic_reports_preserve_running_and_next_step_once(
        fleet, postgres_store, monkeypatch, tmp_path, kind, receipt):
    service, robot, resources, _, sessions, frames, _ = fleet
    task, first = await start(fleet, receipt)
    frame = (dict(type='readiness', busy=True, nav_server_ready=True,
                  brain_server_ready=True, brain_engine='ready') if kind == 'readiness'
             else dict(type='telemetry', battery=85, location='test-table', observed_at='fixture-time'))
    field = 'busy' if kind == 'readiness' else 'location'
    await robot.handle(sessions['A'], frame)
    before = await service.get(USER, task.task_id)
    old_version = await postgres_store.pool.fetchval(
        "SELECT version FROM ra_robot_bindings WHERE binding_id='test-binding'")
    read = resources.snapshot
    async def report_between_snapshot_and_validation(*args, **kwargs):
        snapshot = await read(*args, **kwargs)
        # Reproduce the old race if dispatch still reads resources while awaiting
        # a receipt. All reported values are identical to the already stored ones.
        await robot.handle(sessions['A'], frame)
        return snapshot
    with monkeypatch.context() as patch:
        patch.setattr(resources, 'snapshot', report_between_snapshot_and_validation)
        for _ in range(10):
            await robot.handle(sessions['A'], frame)
            await service.dispatch_next('test-deployment')
            assert await service.get(USER, task.task_id) == before
            assert len(frames['A']) == 1
    row = await postgres_store.pool.fetchrow(
        "SELECT version,observations FROM ra_robot_bindings WHERE binding_id='test-binding'")
    observations = json.loads(row['observations'])
    assert row['version'] > old_version
    assert observations[field]['value'] == frame[field]
    assert observations[field]['connection_generation'] == sessions['A'].generation
    await robot.handle(sessions['A'], dict(type='cmd_status', cmd_id=first['cmd_id'], state='succeeded'))
    await robot.handle(sessions['A'], dict(type='readiness', busy=False))
    await asyncio.gather(*(service.dispatch_next('test-deployment') for _ in range(3)))
    assert len(frames['A']) == 2 and frames['A'][1]['skill'] == 'vla_skill'
    assert frames['A'][1]['cmd_id'] != first['cmd_id']
    await robot.handle(sessions['A'], dict(type='cmd_status', cmd_id=frames['A'][1]['cmd_id'], state='succeeded'))
    completed = await service.get(USER, task.task_id)
    assert completed.state == 'SUCCEEDED' and completed.allocation_robot_binding_id is None
    (tmp_path / 'ordinary-report-flow.json').write_text(json.dumps(
        dict(report=frame, before=asdict(before), completed=asdict(completed),
             commands=frames['A'], saved_observation=observations[field]),
        ensure_ascii=False, indent=2, default=str), encoding='utf-8')


@pytest.mark.parametrize('restriction', ['navigation', 'brain', 'place', 'skill', 'expired', 'authorization'])
async def test_newest_state_is_checked_after_current_command_finishes(fleet, postgres_store, restriction):
    service, robot, _, _, sessions, frames, _ = fleet
    task, first = await start(fleet)
    if restriction == 'authorization':
        service.policy.result = 'deny'
    elif restriction == 'expired':
        await postgres_store.pool.execute("""UPDATE ra_robot_bindings SET observations=
            jsonb_set(observations, '{brain_server_ready,received_at}',
                      to_jsonb('2000-01-01T00:00:00+00:00'::text)) WHERE binding_id='test-binding'""")
    else:
        values = {'navigation': {'nav_server_ready': False}, 'brain': {'brain_server_ready': False},
                  'place': {'waypoint_names': ['different']}, 'skill': {'brain_skills': []}}
        await robot.handle(sessions['A'], dict(type='readiness', **values[restriction]))
    await service.dispatch_next('test-deployment')
    assert (await service.get(USER, task.task_id)).state == 'RUNNING'
    await robot.handle(sessions['A'], dict(type='cmd_status', cmd_id=first['cmd_id'], state='succeeded'))
    await service.dispatch_next('test-deployment')
    blocked = await service.get(USER, task.task_id)
    assert blocked.state == 'BLOCKED' and blocked.reason_code
    if restriction in {'navigation', 'brain', 'expired'}:
        assert blocked.reason_code == 'READINESS_UNKNOWN'
    elif restriction == 'authorization':
        assert blocked.reason_code == 'AUTHORIZATION_DENY'
    assert len(frames['A']) == 1
    assert blocked.result_summary[0]['outcome'] == 'SUCCEEDED'


@pytest.mark.parametrize('fault', ['cancel', 'disconnect', 'disabled', 'unknown_command'])
async def test_waiting_for_receipt_does_not_hide_real_control_events(fleet, postgres_store, fault):
    service, robot, _, _, sessions, frames, _ = fleet
    task, first = await start(fleet)
    await robot.handle(sessions['A'], dict(type='readiness', busy=True))
    await service.dispatch_next('test-deployment')
    if fault == 'cancel':
        current = await service.get(USER, task.task_id)
        await service.cancel(USER, task.task_id, current.version, 'stop')
        assert frames['A'][-1] == {'type': 'cancel', 'cmd_id': first['cmd_id']}
        assert (await service.get(USER, task.task_id)).state == 'CANCEL_REQUESTED'
        await robot.handle(sessions['A'], dict(type='cmd_status', cmd_id=first['cmd_id'], state='failed'))
    elif fault == 'disconnect':
        await robot.unregister(sessions['A'])
    elif fault == 'disabled':
        await postgres_store.pool.execute(
            "UPDATE ra_robot_bindings SET enabled=false,version=version+1 WHERE binding_id='test-binding'")
    else:
        await robot.handle(sessions['A'], dict(type='hello', robot_id='test-robot', busy_cmd_id='unknown-command'))
    await service.dispatch_next('test-deployment')
    result = await service.get(USER, task.task_id)
    expected = {'cancel': ('CANCELLED', ''), 'disconnect': ('BLOCKED', 'RESULT_UNKNOWN'),
                'disabled': ('CANCELLED', 'ROBOT_DISABLED'), 'unknown_command': ('BLOCKED', 'UNTRACKED_EXECUTION')}
    assert (result.state, result.reason_code) == expected[fault]
    assert len([frame for frame in frames['A'] if frame['type'] == 'command']) == 1


async def test_old_scan_cannot_recheck_a_command_already_sent(fleet, monkeypatch):
    from contextlib import asynccontextmanager
    service, robot, resources, _, sessions, frames, connect = fleet
    await connect('A')
    task = await submit_and_plan(service, USER, INPUT, 'old-scan')
    entered, resume = asyncio.Event(), asyncio.Event()
    original_uow, scan_number = service.store.system_unit_of_work, 0

    @asynccontextmanager
    async def pause_old_scan():
        nonlocal scan_number
        scanned = False
        async with original_uow() as tx:
            original_list = tx.list_tasks
            async def listing():
                nonlocal scanned
                result = await original_list()
                scanned = True
                return result
            tx.list_tasks = listing
            yield tx
        if scanned and asyncio.current_task().get_name() == 'old-report-scan':
            scan_number += 1
            # First list checks disabled robots. Second list is the actual scan.
            if scan_number == 2:
                entered.set()
                await resume.wait()

    read = resources.snapshot
    async def report_during_read(*args, **kwargs):
        value = await read(*args, **kwargs)
        await robot.handle(sessions['A'], dict(type='readiness', busy=True))
        return value

    with monkeypatch.context() as patch:
        patch.setattr(service.store, 'system_unit_of_work', pause_old_scan)
        old = asyncio.create_task(service.dispatch_next('test-deployment'), name='old-report-scan')
        try:
            await asyncio.wait_for(entered.wait(), 3)
            await service.dispatch_next('test-deployment')
            assert len(frames['A']) == 1
            before = await service.get(USER, task.task_id)
            assert before.state == 'RUNNING'
            patch.setattr(resources, 'snapshot', report_during_read)
            resume.set()
            await asyncio.wait_for(old, 3)
            assert await service.get(USER, task.task_id) == before
            assert len(frames['A']) == 1
        finally:
            resume.set()
            await old
