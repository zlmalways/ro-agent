"""Steps 21-24: real factory/HTTP/WebSocket/DB/browser; explicit local model and simulator."""
import asyncio
from contextlib import asynccontextmanager, suppress
import json

from playwright.async_api import async_playwright, expect
import pytest
from websockets.asyncio.client import ClientConnection

from tests.acceptance.test_scheduling import platform
from tests.acceptance.test_restart_abandonment import task_until
from tests.e2e.test_dashboard import login, submit
from tests.manual.mock_robot_ws import Simulator, command_session

pytestmark = pytest.mark.integration
PLACE = '冰箱旁（模拟）'


class TraceRobot(Simulator):
    def __init__(self, *, fault='', duration=0.2):
        super().__init__('test-robot', 'mock-fridge-map（模拟）', [PLACE], [], duration)
        self.fault = fault
        self.reports, self.commands, self.receipts = [], [], []
        self.executions = 0

    def readiness(self):
        frame = super().readiness()
        if self.fault == 'missing_map':
            frame.pop('map')
        if self.fault == 'not_ready':
            frame['nav_server_ready'] = False
        self.reports.append(frame)
        return frame

    def handle(self, frame, now):
        before = self.active
        if frame.get('type') in {'command', 'cancel'}:
            self.commands.append(frame)
        reply = super().handle(frame, now)
        if before is None and self.active is not None:
            self.executions += 1
        if reply:
            self.receipts.append(reply)
        return reply

    def finish(self, state, detail):
        if self.fault == 'action_failed' and state == 'succeeded':
            state, detail = 'failed', 'simulated navigation failure'
        reply = super().finish(state, detail)
        self.receipts.append(reply)
        return reply


@asynccontextmanager
async def simulated(base, robot):
    worker = asyncio.create_task(command_session(base+'/api/voice/robot/ws', 'device', robot))
    try:
        yield
    finally:
        worker.cancel()
        with suppress(asyncio.CancelledError):
            await worker


async def reported(app):
    context = await app.state.identity.resolve_web('web')
    async with asyncio.timeout(15):
        while True:
            snapshot = await app.state.resources.snapshot(context, 'test-binding')
            if 'preload' in snapshot.readiness_evidence:
                return snapshot
            await asyncio.sleep(0.02)


async def request_task(web, text='移动到'+PLACE, key='move'):
    reply = await web.post('/api/robot-agent/tasks', json={'text':text}, headers={'Idempotency-Key':key})
    assert reply.status_code == 202
    return reply.json()['task_id']


async def save_flow(path, store, robot, prompts, task, **extra):
    row = await store.pool.fetchrow('SELECT state,reason_code FROM ra_tasks WHERE task_id=$1', task['task_id'])
    assert (row['state'], row['reason_code']) == (task['state'], task['reason_code'])
    commands = await store.pool.fetch('SELECT command_id,outcome,dispatch_state FROM ra_commands WHERE task_id=$1', task['task_id'])
    assert {r['command_id'] for r in commands} == {r['command_id'] for r in task['result_summary']}
    path.write_text(json.dumps(dict(reports=robot.reports, model_inputs=prompts, commands=robot.commands,
        receipts=robot.receipts, execution_count=robot.executions, database_commands=[dict(row) for row in commands],
        final=task, **extra), ensure_ascii=False, indent=2), encoding='utf-8')


async def test_web_move_waits_for_final_receipt_and_duplicate_executes_once(configuration, postgres_store, tmp_path, monkeypatch):
    configuration['limits']['freshness_seconds'] = 10
    robot = TraceRobot(duration=1)
    ending, release = asyncio.Event(), asyncio.Event()
    original = ClientConnection.send
    async def hold_success(ws, message, *args, **kwargs):
        frame = json.loads(message)
        if frame.get('state') == 'succeeded' and not ending.is_set():
            ending.set()
            await release.wait()
        return await original(ws, message, *args, **kwargs)
    monkeypatch.setattr(ClientConnection, 'send', hold_success)
    async with platform(configuration, postgres_store, tmp_path, execute=True) as (app, base, web, prompts):
        async with simulated(base, robot), async_playwright() as pw:
            browser = await pw.chromium.launch(channel='msedge')
            try:
                snapshot = await reported(app)
                assert list(snapshot.waypoints) == [PLACE]
                assert await postgres_store.pool.fetchval('SELECT count(*) FROM ra_resource_versions') == 0
                page = await browser.new_page(viewport={'width':1440, 'height':1000})
                errors = []
                page.on('pageerror', lambda error: errors.append(str(error)))
                await page.goto(base.replace('ws:', 'http:'))
                await login(page, 'web')
                await submit(page, '移动到'+PLACE)
                identifier = await page.locator('#task-id').text_content()
                running = await task_until(web, identifier, lambda task: task['state'] == 'RUNNING' and bool(task['result_summary'])
                    and task['result_summary'][0]['outcome'] == 'RUNNING')
                session = app.state.service.robot.sessions[('test-customer', 'test-binding')]
                # Deliberately duplicate the already-sent wire packet, without creating a second DB command.
                await session.send(dict(robot.commands[0]))
                await asyncio.wait_for(ending.wait(), 4)
                await expect(page.locator('#status')).to_have_text('执行中')
                assert (await web.get('/api/robot-agent/tasks/'+identifier)).json()['state'] == 'RUNNING'
                await page.screenshot(path=str(tmp_path/'movement-before-final.png'), full_page=True)
                release.set()
                final = await task_until(web, identifier, lambda task: task['state']=='SUCCEEDED')
                await expect(page.locator('#status')).to_have_text('已完成')
                await expect(page.locator('#plan')).to_contain_text(PLACE)
                await expect(page.locator('#results')).to_contain_text('机器人报告成功')
                await page.screenshot(path=str(tmp_path/'movement-success-desktop.png'), full_page=True)
                await page.set_viewport_size({'width':390, 'height':844})
                await page.screenshot(path=str(tmp_path/'movement-success-mobile.png'), full_page=True)
                assert not errors and robot.executions == 1
                assert len(prompts) == 1 and prompts[0]['waypoints'] == [PLACE]
                assert final['plan_steps'][0]['args'] == {'location':PLACE}
                assert len(final['result_summary']) == 1 and len(robot.commands) == 2
                assert await postgres_store.pool.fetchval('SELECT count(*) FROM ra_execution_guard') == 0
                await save_flow(tmp_path/'movement-success.json', postgres_store, robot, prompts, final, before_receipt=running, page_errors=errors)
            finally:
                release.set()
                await browser.close()


@pytest.mark.parametrize('fault,reason', [('missing_map','PLAN_INVALID'), ('not_ready','READINESS_UNKNOWN'),
                                       ('unknown_place','PLAN_INVALID'), ('permission_denied','AUTHORIZATION_DENY')])
async def test_invalid_movement_never_reaches_robot(configuration, postgres_store, tmp_path, fault, reason):
    configuration['limits']['freshness_seconds'] = 10
    if fault == 'permission_denied':
        configuration['policy']['actions']['test-customer']['move_to'] = 'deny'
    robot = TraceRobot(fault=fault)
    async def propose(context):
        place = '不存在的地点' if fault == 'unknown_place' else PLACE
        return {'outcome':'plan', 'steps':[dict(step_id='s1', action='move_to', args={'location':place}, depends_on=[])]}
    async with platform(configuration, postgres_store, tmp_path, execute=True, model_hook=propose) as (app, base, web, prompts):
        async with simulated(base, robot):
            await reported(app)
            identifier = await request_task(web)
            final = await task_until(web, identifier, lambda task: task['state'] in {'BLOCKED','FAILED'})
            assert final['reason_code'] == reason and not final['result_summary']
            assert not robot.commands and robot.executions == 0
            assert await postgres_store.pool.fetchval('SELECT count(*) FROM ra_commands') == 0
            await save_flow(tmp_path/(fault+'.json'), postgres_store, robot, prompts, final)


@pytest.mark.parametrize('fault', ['action_failed', 'cancel'])
async def test_failure_and_cancel_end_without_false_success(configuration, postgres_store, tmp_path, fault):
    configuration['limits']['freshness_seconds'] = 10
    robot = TraceRobot(fault=fault, duration=60 if fault=='cancel' else 0.2)
    async with platform(configuration, postgres_store, tmp_path, execute=True) as (app, base, web, prompts):
        async with simulated(base, robot):
            await reported(app)
            identifier = await request_task(web)
            if fault == 'cancel':
                # RUNNING also covers PREPARED/DISPATCHING. This scenario must
                # cancel an action the simulated robot has actually started.
                async with asyncio.timeout(4):
                    while not robot.receipts or robot.receipts[-1]['state'] != 'running':
                        await asyncio.sleep(0.01)
                running = await task_until(web, identifier, lambda task: task['state']=='RUNNING'
                    and task['result_summary'][0]['outcome']=='RUNNING')
                response = await web.post('/api/robot-agent/tasks/'+identifier+'/cancel',
                    json={'expected_version':running['version']}, headers={'Idempotency-Key':'stop'})
                assert response.status_code == 200
                try:
                    final = await task_until(web, identifier, lambda task: task['state']=='CANCELLED')
                finally:
                    observed = (await web.get('/api/robot-agent/tasks/'+identifier)).json()
                    await save_flow(tmp_path/'cancel-observed.json', postgres_store, robot, prompts, observed,
                                    before_cancel=running, cancel_response=response.json())
                assert [c['type'] for c in robot.commands] == ['command','cancel']
            else:
                final = await task_until(web, identifier, lambda task: task['reason_code']=='ACTION_FAILED')
                assert final['state'] == 'BLOCKED'
            assert robot.receipts[-1]['state']=='failed' and robot.receipts[-1]['progress']==1.0
            assert robot.active is None and robot.executions==1
            assert final['result_summary'][0]['outcome']=='FAILED'
            await save_flow(tmp_path/(fault+'.json'), postgres_store, robot, prompts, final)


async def test_initial_and_expired_status_block_and_fresh_status_requires_new_submission(configuration, postgres_store, tmp_path, monkeypatch):
    configuration['limits']['freshness_seconds'] = 10
    robot = TraceRobot()
    late, release = asyncio.Event(), asyncio.Event()
    original = ClientConnection.send
    seen = 0
    async def delay_second_report(ws, message, *args, **kwargs):
        nonlocal seen
        frame=json.loads(message)
        if frame.get('type')=='readiness':
            seen += 1
            if seen == 2:
                late.set()
                await release.wait()
        return await original(ws, message, *args, **kwargs)
    monkeypatch.setattr(ClientConnection, 'send', delay_second_report)
    async with platform(configuration, postgres_store, tmp_path, execute=True) as (app, base, web, prompts):
        async with simulated(base, robot):
            async with asyncio.timeout(4):
                while not app.state.service.robot.connected_robots('test-customer'):
                    await asyncio.sleep(0.01)
            early_id = await request_task(web, key='before-report')
            early = await task_until(web, early_id, lambda task:task['state']=='BLOCKED')
            assert early['reason_code']=='ROBOT_STATE_UNKNOWN' and not prompts
            await reported(app)
            try:
                await asyncio.wait_for(late.wait(), 14)
                await asyncio.sleep(0.15)
                stale_id = await request_task(web, key='expired')
                stale = await task_until(web, stale_id, lambda task:task['state']=='BLOCKED')
                assert stale['reason_code']=='ROBOT_STATE_UNKNOWN' and not robot.commands and not prompts
                release.set()
                context=await app.state.identity.resolve_web('web')
                async with asyncio.timeout(4):
                    while not (await app.state.resources.snapshot(context,'test-binding')).waypoints:
                        await asyncio.sleep(0.02)
                await asyncio.sleep(0.25)
                assert (await web.get('/api/robot-agent/tasks/'+stale_id)).json()['state']=='BLOCKED'
                # Existing admission semantics: retry deliberately as a NEW request;
                # replaying an old idempotency key is only a lookup for this reason.
                replay = await web.post('/api/robot-agent/tasks', json={'text':'移动到'+PLACE}, headers={'Idempotency-Key':'expired'})
                assert replay.json()['task_id']==stale_id and replay.json()['state']=='BLOCKED'
                new_id=await request_task(web,key='explicit-new-request')
                final=await task_until(web,new_id,lambda task:task['state']=='SUCCEEDED')
                assert len(prompts)==1 and robot.executions==1
                await save_flow(tmp_path/'freshness.json',postgres_store,robot,prompts,final,initial=early,expired=stale)
            finally:
                release.set()
