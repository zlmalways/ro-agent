"""Real PostgreSQL, production Resources/transport, explicitly simulated connections/model."""
from tests.fixtures.submission import submit_and_plan, answer_and_plan
from dataclasses import replace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

from robot_agent.adapters.dlv0 import Dlv0Robot
from robot_agent.application.resources import Resources
from robot_agent.domain.models import CandidatePlan, DependencyUnavailable, TaskInput, TrustedContext
from tests.fixtures.minimal import build_service


pytestmark = pytest.mark.integration
USER = TrustedContext("test-customer", "user", "test-user")
ADMIN = replace(USER, roles=frozenset({"maintainer"}))
INPUT = TaskInput("request", "web", "移动到桌边", "")


@pytest_asyncio.fixture
async def fleet(postgres_store):
    service, _, _ = build_service()
    service.store = postgres_store
    robot = Dlv0Robot(service=service, write_gate=service.ownership, max_frame_bytes=8192)
    resources = Resources(postgres_store, freshness_seconds=60, max_entries=100,
                          connected_robots=robot.connected_robots)
    service.resources, service.robot = resources, robot
    bindings, sessions, frames = {}, {}, {}
    for label, customer, identifier in [('A', USER.customer_id, 'test-binding'),
                                        ('B', USER.customer_id, 'binding-b'),
                                        ('C', 'other-customer', 'binding-c')]:
        binding = dict(customer_id=customer, binding_id=identifier, wire_robot_id='robot-' + label,
                       allowed_actions=['move_to', 'vla_skill'], token_env='TEST_' + label)
        if label == 'A':
            binding['wire_robot_id'] = 'test-robot'
        async with postgres_store.system_unit_of_work() as tx:
            await tx.provision_binding(binding)
        admin = replace(ADMIN, customer_id=customer)
        await resources.put(admin, identifier, 'environment', {'test-table': '桌边 ' + label}, ('fixture:map',))
        await resources.put(admin, identifier, 'capability', {'test-pick': '拿水 ' + label}, ('fixture:skill',))
        bindings[label], frames[label] = binding, []

    async def connect(label):
        binding = bindings[label]
        async def send(frame):
            frames[label].append(frame)
        session = await robot.register(binding, {'type': 'hello', 'robot_id': binding['wire_robot_id'],
                                                 'skills': ['move_to', 'vla_skill']}, send)
        await robot.handle(session, {'type': 'readiness', 'waypoint_names': ['test-table'],
                                    'brain_skills': [{'skill_id': 'test-pick'}], 'busy': False,
                                    'nav_server_ready': True, 'brain_server_ready': True, 'brain_engine': 'ready'})
        sessions[label] = session
        return session
    try:
        yield service, robot, resources, bindings, sessions, frames, connect
    finally:
        await service.close_work()


async def test_connections_return_customer_ids_and_generations_only(fleet):
    _, robot, _, bindings, sessions, _, connect = fleet
    await connect('A')
    await connect('C')
    result = robot.connected_robots(USER.customer_id)
    assert result == {'test-binding': sessions['A'].generation}
    result.clear()
    assert robot.connected_robots(USER.customer_id)
    old = sessions['A']
    await connect('A')
    await robot.unregister(old)
    assert robot.connected_robots(USER.customer_id) == {'test-binding': sessions['A'].generation}
    await robot.unregister(sessions['A'])
    assert robot.connected_robots(USER.customer_id) == {}


async def test_offline_registration_and_other_customer_not_candidates(fleet):
    _, _, resources, _, _, _, connect = fleet
    await connect('A')
    await connect('C')
    details = await resources.online_robot_details(USER)
    assert [s.robot_binding_id for s in details] == ['test-binding']
    assert details[0].environment_labels == {'test-table': '桌边 A'}
    assert details[0].capability_labels == {'test-pick': '拿水 A'}
    assert (await resources.snapshot(USER, 'binding-b')).robot_binding_id == 'binding-b'
    with pytest.raises(DependencyUnavailable):
        await resources.snapshot(USER, 'binding-c')


@pytest.mark.parametrize('excluded', ['disabled', 'isolated', 'old_generation', 'db_only'])
async def test_unusable_or_stale_binding_excluded(fleet, postgres_store, excluded):
    _, robot, resources, _, _, _, connect = fleet
    await connect('A')
    if excluded == 'disabled':
        await postgres_store.pool.execute("UPDATE ra_robot_bindings SET enabled=false WHERE binding_id='test-binding'")
    elif excluded == 'isolated':
        await postgres_store.pool.execute("UPDATE ra_robot_bindings SET isolation_reason='EVIDENCE_CONFLICT' WHERE binding_id='test-binding'")
    elif excluded == 'old_generation':
        await postgres_store.pool.execute("UPDATE ra_robot_bindings SET connection_generation=connection_generation+1 WHERE binding_id='test-binding'")
    else:
        robot.sessions.clear()  # Stored connected=true survives, but there is no current WS.
    assert await resources.online_robot_details(USER) == []


@pytest.mark.parametrize('change', ['disconnect', 'reconnect'])
async def test_connection_change_during_read_does_not_return_old_candidate(fleet, monkeypatch, change):
    _, robot, resources, _, sessions, _, connect = fleet
    await connect('A')
    original = resources.snapshot
    async def racing_read(context, binding_id=None):
        snapshot = await original(context, binding_id)
        if change == 'disconnect':
            await robot.unregister(sessions['A'])
        else:
            await connect('A')
        return snapshot
    monkeypatch.setattr(resources, 'snapshot', racing_read)
    assert await resources.online_robot_details(USER) == []


async def test_stale_navigation_is_not_made_ready_by_selection(fleet, postgres_store):
    service, _, _, _, _, frames, connect = fleet
    await connect('A')
    await postgres_store.pool.execute("""UPDATE ra_robot_bindings SET observations=
        jsonb_set(observations, '{nav_server_ready,received_at}', to_jsonb('2000-01-01T00:00:00+00:00'::text))
        WHERE binding_id='test-binding'""")
    view = await submit_and_plan(service, USER, INPUT, 'stale-nav')
    assert view.state == 'BLOCKED' and view.reason_code == 'READINESS_UNKNOWN'
    assert not any(frames.values())


@pytest.mark.parametrize('online,reason', [((), 'NO_ONLINE_ROBOT'), (('A', 'B'), 'MULTI_ROBOT_SCHEDULING_NOT_IMPLEMENTED')])
async def test_no_choice_is_saved_without_model_call_and_duplicate_does_not_retry(fleet, online, reason):
    service, _, _, _, _, frames, connect = fleet
    for label in online:
        await connect(label)
    service.planner.plan = AsyncMock(side_effect=AssertionError('must not plan'))
    first = await submit_and_plan(service, USER, INPUT, 'same')
    if not online:
        await connect('A')
    duplicate = await submit_and_plan(service, USER, INPUT, 'same')
    assert first == duplicate and first.state == 'BLOCKED' and first.reason_code == reason
    service.planner.plan.assert_not_called()
    assert not any(frames.values())


async def test_new_tasks_select_current_robot_independently(fleet, postgres_store):
    service, robot, _, _, sessions, _, connect = fleet
    await connect('A')
    first = await submit_and_plan(service, USER, INPUT, 'first')
    await service.cancel(USER, first.task_id, first.version, 'cancel-first')
    await robot.unregister(sessions['A'])
    await connect('B')
    second = await submit_and_plan(service, USER, INPUT, 'second')
    async with postgres_store.system_unit_of_work() as tx:
        a, b = await tx.load_task(first.task_id), await tx.load_task(second.task_id)
    assert a.snapshot.robot_binding_id == 'test-binding'
    assert b.snapshot.robot_binding_id == 'binding-b'


@pytest.mark.parametrize('answer_online', ['B', 'none', 'both'])
async def test_clarification_reselects_and_saves_answer_once(fleet, postgres_store, answer_online):
    service, robot, _, _, sessions, _, connect = fleet
    await connect('A')
    original = service.planner.plan
    service.planner.plan = AsyncMock(return_value=CandidatePlan('clarify', question='去哪里？'))
    asked = await submit_and_plan(service, USER, INPUT, 'ask')
    await robot.unregister(sessions['A'])
    if answer_online != 'none':
        await connect('B')
    if answer_online == 'both':
        await connect('A')
    service.planner.plan = AsyncMock(wraps=original)
    answered = await answer_and_plan(service, USER, asked.task_id, asked.clarification_id, '桌边', asked.version, 'answer')
    repeated = await answer_and_plan(service, USER, asked.task_id, asked.clarification_id, '桌边', asked.version, 'answer')
    assert answered == repeated
    async with postgres_store.system_unit_of_work() as tx:
        stored = await tx.load_task(asked.task_id)
    assert stored.answers == ('桌边',)
    if answer_online == 'B':
        assert stored.snapshot.robot_binding_id == 'binding-b' and stored.state == 'READY'
        assert service.planner.plan.call_args.args[1] == ['桌边']
        assert service.planner.plan.await_count == 1
    else:
        assert stored.state == 'BLOCKED' and not stored.plan
        service.planner.plan.assert_not_called()


async def test_approved_plan_and_both_steps_remain_on_a_when_b_connects(fleet):
    service, robot, _, _, sessions, frames, connect = fleet
    service.policy.result = 'require_confirmation'
    await connect('A')
    task = await submit_and_plan(service, USER, INPUT, 'approve')
    await connect('B')
    ready = await service.decide(USER, task.task_id, task.version, 'approve', 'decision', plan_id=task.plan_id)
    assert ready.state == 'READY'
    for index in (0, 1):
        await service.dispatch_next('test-deployment')
        assert len(frames['A']) == index + 1 and not frames['B']
        command = frames['A'][index]
        await robot.handle(sessions['A'], dict(type='cmd_status', cmd_id=command['cmd_id'], state='succeeded'))
    completed = await service.get(USER, task.task_id)
    assert completed.state == 'SUCCEEDED'
    assert {c['robot_binding_id'] for c in completed.result_summary} == {'test-binding'}


@pytest.mark.parametrize('change', ['disconnect', 'navigation'])
async def test_a_unavailable_after_planning_is_not_replaced_by_b(fleet, change):
    service, robot, _, _, sessions, frames, connect = fleet
    await connect('A')
    task = await submit_and_plan(service, USER, INPUT, 'blocked')
    await connect('B')
    if change == 'disconnect':
        await robot.unregister(sessions['A'])
    else:
        await robot.handle(sessions['A'], dict(type='readiness', nav_server_ready=False))
    await service.dispatch_next('test-deployment')
    assert not any(frames.values())
    assert (await service.get(USER, task.task_id)).state == 'BLOCKED'


async def test_missing_reader_fails_closed_and_database_failure_is_not_no_candidate(postgres_store):
    resources = Resources(postgres_store, freshness_seconds=60, max_entries=100)
    assert await resources.online_robot_details(USER) == []
    resources.connected_robots = lambda customer: {'test-binding': 1}
    resources.snapshot = AsyncMock(side_effect=RuntimeError('storage failed'))
    with pytest.raises(RuntimeError, match='storage failed'):
        await resources.online_robot_details(USER)


async def test_busy_sole_robot_keeps_second_task_waiting_until_first_finishes(fleet, postgres_store):
    service, robot, _, _, sessions, frames, connect = fleet
    await connect('A')
    first = await submit_and_plan(service, USER, INPUT, 'first-busy')
    await service.dispatch_next('test-deployment')
    command = frames['A'][0]
    await robot.handle(sessions['A'], dict(type='readiness', busy=True, busy_cmd_id=command['cmd_id']))
    second = await submit_and_plan(service, USER, INPUT, 'second-busy')
    assert second.state == 'QUEUED' and second.plan_id is None
    await service.dispatch_next('test-deployment')
    assert len(frames['A']) == 1
    assert (await service.get(USER, second.task_id)).result_summary == ()
    await robot.handle(sessions['A'], dict(type='cmd_status', cmd_id=command['cmd_id'], state='succeeded'))
    await service.dispatch_next('test-deployment')
    assert frames['A'][1]['skill'] == 'vla_skill'
    await robot.handle(sessions['A'], dict(type='cmd_status', cmd_id=frames['A'][1]['cmd_id'], state='succeeded'))
    assert (await service.get(USER, first.task_id)).state == 'SUCCEEDED'
    await robot.handle(sessions['A'], dict(type='readiness', busy=False))
    await service.schedule_pending('test-deployment')
    await service.dispatch_next('test-deployment')
    assert len(frames['A']) == 3 and not frames['B']
    assert (await service.get(USER, second.task_id)).result_summary[0]['command_id'] == frames['A'][2]['cmd_id']
    assert await postgres_store.pool.fetchval('SELECT task_id FROM ra_execution_guard') == second.task_id


@pytest.mark.parametrize('count', [0, 1, 2])
async def test_legacy_resource_endpoints_keep_unique_registered_binding_rule(postgres_store, count):
    resources = Resources(postgres_store, freshness_seconds=60, max_entries=100)
    if count == 0:
        await postgres_store.pool.execute("UPDATE ra_robot_bindings SET enabled=false")
    elif count == 2:
        async with postgres_store.system_unit_of_work() as tx:
            await tx.provision_binding(dict(customer_id=USER.customer_id, binding_id='b', wire_robot_id='b',
                                           allowed_actions=['move_to'], token_env='TEST_B'))
    for call in (lambda: resources.read(USER, 'environment'), lambda: resources.binding_for(USER),
                 lambda: resources.snapshot(USER)):
        if count == 1:
            await call()
        else:
            with pytest.raises(DependencyUnavailable, match='ROBOT_BINDING_UNAVAILABLE'):
                await call()
