"""C1 S1-S4: real deployment factory, locks, HTTP/WS/DB/browser; local model fixture only."""
import asyncio
from contextlib import asynccontextmanager, suppress
from copy import deepcopy
import json
import os
import socket

from fastapi import FastAPI, Request
import httpx
from playwright.async_api import async_playwright, expect
import pytest
from websockets.asyncio.client import connect

from robot_agent.deployment import build_app
from tests.e2e.test_dashboard import login, submit
from tests.fixtures.submission import wait_for_task
from tests.integration.test_robot_ws import live_app
from tests.manual.mock_robot_ws import Simulator, command_session


pytestmark = pytest.mark.integration


def add_b(configuration):
    row = deepcopy(configuration['robots'][0])
    row.update(binding_id='binding-b', wire_robot_id='robot-b', token_env='TOKEN_B')
    configuration['robots'].append(row)


@asynccontextmanager
async def platform(configuration, store, tmp_path, *, execute=False, model_hook=None, two_steps=False, environment_extra=None):
    config = deepcopy(configuration)
    config['database']['schema'] = await store.pool.fetchval('SELECT current_schema()')
    config['resources'] = {'use_reported_waypoints': True}
    config['allow_external_calls'] = True
    if execute:
        config['deployment'].update(execution_enabled=True, host_id=socket.gethostname(),
                                    lock_path=str(tmp_path / 'owner.lock'), advisory_key=490043)
    prompts = []
    model = FastAPI()
    @model.post('/chat/completions')
    async def propose(request: Request):
        payload = await request.json()
        context = json.loads(payload['messages'][-1]['content'])
        prompts.append(context)
        override = await model_hook(context) if model_hook else None
        candidate = override if override is not None else {'outcome': 'plan', 'steps': [{'step_id': 's1', 'action': 'move_to',
                     'args': {'location': context['waypoints'][0]}, 'depends_on': []}]}
        if two_steps:
            candidate['steps'].append({'step_id': 's2', 'action': 'move_to',
                                      'args': {'location': context['waypoints'][0]}, 'depends_on': ['s1']})
        return {'choices': [{'message': {'content': json.dumps(candidate, ensure_ascii=False)}}]}
    async with live_app(model) as model_url:
        env = {'TEST_DSN': os.environ['RO_AGENT_TEST_DSN'], 'TEST_USER_TOKEN': 'web',
               'TEST_ROBOT_TOKEN': 'device', 'TOKEN_B': 'device-b', 'DEEPLEAP_LLM_PROVIDER': 'gpt',
               'GPT_BASE_URL': model_url.replace('ws:', 'http:'), 'GPT_MODEL': 'local-protocol-fixture',
               'GPT_API_KEY': 'test-only', 'GPT_TIMEOUT_S': '5'}
        env.update(environment_extra or {})
        app = build_app(config, environment=env)
        async with app.router.lifespan_context(app), live_app(app) as base:
            async with httpx.AsyncClient(base_url=base.replace('ws:', 'http:'), trust_env=False,
                                          headers={'Authorization': 'Bearer web'}) as web:
                yield app, base, web, prompts


@asynccontextmanager
async def robot_link(app, base, *, name='test-robot', token='device', binding='test-binding', busy_cmd_id='', user_token='web'):
    async with connect(base + '/api/voice/robot/ws?robot_id=' + name,
                       additional_headers={'x-robot-token': token}, proxy=None) as ws:
        await ws.recv()
        await ws.send(json.dumps(dict(type='hello', robot_id=name, skills=['move_to'], busy_cmd_id=busy_cmd_id)))
        await ws.recv()
        await ws.send(json.dumps(dict(type='readiness', busy=bool(busy_cmd_id), map='模拟地图', nav_server_ready=True,
                                      waypoint_names=['研发桌一' if binding == 'test-binding' else 'B的地点'])))
        async with asyncio.timeout(5):
            while True:
                context = await app.state.identity.resolve_web(user_token)
                if (await app.state.resources.snapshot(context, binding)).waypoints:
                    break
                await asyncio.sleep(0.01)
        yield ws


@pytest.mark.parametrize('count', [0, 1, 2, 10])
def test_formal_configuration_has_no_single_robot_limit(configuration, count):
    row = configuration['robots'][0]
    configuration['robots'] = [dict(row, binding_id=f'b-{i}', wire_robot_id=f'r-{i}', token_env=f'T-{i}')
                               for i in range(count)]
    env = {'TEST_DSN': 'postgresql://localhost/unused_test', **{f'T-{i}': f'secret-{i}' for i in range(count)}}
    assert build_app(configuration, environment=env)


@pytest.mark.parametrize('collision', ['binding', 'credential'])
def test_formal_configuration_rejects_ambiguous_robot_identity(configuration, collision):
    add_b(configuration)
    if collision == 'binding':
        configuration['robots'][1]['binding_id'] = 'test-binding'
    env = {'TEST_DSN': 'postgresql://localhost/unused_test', 'TEST_ROBOT_TOKEN': 'one',
           'TOKEN_B': 'one' if collision == 'credential' else 'two'}
    with pytest.raises(ValueError, match='duplicate robot ' + collision):
        build_app(configuration, environment=env)


@pytest.mark.parametrize('count,reason,message', [
    (0, 'NO_ONLINE_ROBOT', '没有在线机器人'),
    (2, 'MULTI_ROBOT_SCHEDULING_NOT_IMPLEMENTED', '多机器人调度尚未实现'),
])
async def test_formal_block_is_202_persisted_and_visible(configuration, postgres_store, tmp_path, count, reason, message):
    from contextlib import AsyncExitStack
    add_b(configuration)
    async with platform(configuration, postgres_store, tmp_path) as (app, base, web, prompts), AsyncExitStack() as stack:
        if count:
            await stack.enter_async_context(robot_link(app, base))
            await stack.enter_async_context(robot_link(app, base, name='robot-b', token='device-b', binding='binding-b'))
        first = await web.post('/api/robot-agent/tasks', json={'text': '移动到研发桌一'}, headers={'Idempotency-Key': 'once'})
        assert first.status_code == 202
        assert first.json()['state'] == 'RECEIVED'
        task = await wait_for_task(web, first.json()['task_id'])
        assert task['state'] == 'BLOCKED' and task['reason_code'] == reason
        repeated = await web.post('/api/robot-agent/tasks', json={'text': '移动到研发桌一'}, headers={'Idempotency-Key': 'once'})
        assert repeated.json() == task
        assert not prompts
        assert await postgres_store.pool.fetchval('SELECT count(*) FROM ra_tasks') == 1
        assert await postgres_store.pool.fetchval('SELECT count(*) FROM ra_commands') == 0
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(channel='msedge')
            try:
                page = await browser.new_page()
                await page.goto(base.replace('ws:', 'http:'))
                await login(page, 'web')
                await submit(page, '移动到研发桌一')
                await expect(page.locator('#reason')).to_contain_text(message)
                await expect(page.locator('#results')).to_have_text('尚无机器人反馈。')
                await page.screenshot(path=str(tmp_path / f'scheduling-{count}.png'), full_page=True)
            finally:
                await browser.close()


async def test_formal_web_to_simulator_success_is_persisted(configuration, postgres_store, tmp_path):
    add_b(configuration)  # B is registered but offline throughout.
    class ObservedSimulator(Simulator):
        def __init__(self):
            super().__init__('test-robot', '模拟地图', ['研发桌一'], [], 0.05)
            self.commands = []
        def handle(self, frame, now=None):
            if frame['type'] == 'command':
                self.commands.append(frame)
            return super().handle(frame, now)
    robot = ObservedSimulator()
    async with platform(configuration, postgres_store, tmp_path, execute=True) as (app, base, web, prompts):
        worker = asyncio.create_task(command_session(base + '/api/voice/robot/ws', 'device', robot, interval=10))
        try:
            async with asyncio.timeout(15):
                while True:
                    context = await app.state.identity.resolve_web('web')
                    details = await app.state.resources.online_robot_details(context)
                    if details and details[0].waypoints:
                        break
                    await asyncio.sleep(0.01)
            async with async_playwright() as pw:
                browser = await pw.chromium.launch(channel='msedge')
                try:
                    page = await browser.new_page()
                    errors = []
                    page.on('pageerror', lambda error: errors.append(str(error)))
                    await page.goto(base.replace('ws:', 'http:'))
                    await login(page, 'web')
                    await submit(page, '移动到研发桌一')
                    await expect(page.locator('#status')).to_have_text('已完成', timeout=10000)
                    await expect(page.locator('#results')).to_contain_text('机器人报告成功')
                    identifier = await page.locator('#task-id').text_content()
                    view = (await web.get('/api/robot-agent/tasks/' + identifier)).json()
                    assert view['state'] == 'SUCCEEDED' and not errors
                    assert prompts[0]['waypoints'] == ['研发桌一']
                    assert len(robot.commands) == 1
                    assert robot.commands[0]['skill'] == 'move_to'
                    assert robot.commands[0]['args'] == {'location': '研发桌一'}
                    assert view['result_summary'][0]['robot_binding_id'] == 'test-binding'
                    assert view['result_summary'][0]['evidence_ids']
                    assert await postgres_store.pool.fetchval('SELECT state FROM ra_tasks WHERE task_id=$1', identifier) == 'SUCCEEDED'
                    assert await postgres_store.pool.fetchval('SELECT count(*) FROM ra_commands') == 1
                    (tmp_path / 'formal-flow.json').write_text(json.dumps(
                        {'model_input': prompts, 'commands': robot.commands, 'task': view}, ensure_ascii=False, indent=2), encoding='utf-8')
                    await page.screenshot(path=str(tmp_path / 'formal-completed.png'), full_page=True)
                finally:
                    await browser.close()
        finally:
            worker.cancel()
            with suppress(asyncio.CancelledError):
                await worker


async def test_formal_restart_never_resends_unknown_command(configuration, postgres_store, tmp_path):
    # Step 17 supersedes the earlier S4 restart continuation; evidence stays queryable.
    async with platform(configuration, postgres_store, tmp_path, execute=True) as (app, base, web, _):
        async with robot_link(app, base) as ws:
            created = await web.post('/api/robot-agent/tasks', json={'text': '移动到研发桌一'}, headers={'Idempotency-Key': 'restart'})
            assert created.status_code == 202
            identifier = created.json()['task_id']
            command = json.loads(await asyncio.wait_for(ws.recv(), 5))
            assert command['type'] == 'command'
        async with asyncio.timeout(5):
            while (await web.get('/api/robot-agent/tasks/' + identifier)).json()['reason_code'] != 'RESULT_UNKNOWN':
                await asyncio.sleep(0.01)
    async with platform(configuration, postgres_store, tmp_path, execute=True) as (app, base, web, prompts):
        async with robot_link(app, base, busy_cmd_id=command['cmd_id']) as ws:
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(ws.recv(), 0.3)
            assert not prompts
            await ws.send(json.dumps({'type': 'cmd_status', 'cmd_id': command['cmd_id'], 'state': 'succeeded'}))
            async with asyncio.timeout(5):
                while (await web.get('/api/robot-agent/tasks/' + identifier)).json()['result_summary'][0]['outcome'] != 'SUCCEEDED':
                    await asyncio.sleep(0.01)
            ended = (await web.get('/api/robot-agent/tasks/' + identifier)).json()
            assert ended['state'] == 'CANCELLED' and ended['reason_code'] == 'RESTART_TASK_LOST'
            assert await postgres_store.pool.fetchval('SELECT count(*) FROM ra_commands') == 1


async def test_connected_robot_does_not_hide_real_model_failure(configuration, postgres_store, tmp_path):
    async with platform(configuration, postgres_store, tmp_path) as (app, base, web, _):
        app.state.service.planner.config = None
        async with robot_link(app, base):
            response = await web.post('/api/robot-agent/tasks', json={'text': '移动到研发桌一'}, headers={'Idempotency-Key': 'bad-provider'})
            assert response.status_code == 202 and response.json()['state'] == 'RECEIVED'
            final = await wait_for_task(web, response.json()['task_id'])
            assert final['state'] == 'BLOCKED' and final['reason_code'] == 'PLANNER_UNAVAILABLE'
            assert await postgres_store.pool.fetchval('SELECT count(*) FROM ra_commands') == 0
