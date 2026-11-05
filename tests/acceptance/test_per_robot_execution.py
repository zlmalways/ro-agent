"""C1 M1-M4: actual factory, runtime, HTTP model fixture, WS simulators and browser."""
import asyncio
from contextlib import suppress
import json

from playwright.async_api import async_playwright, expect
import pytest

from tests.acceptance.test_scheduling import add_b, platform, robot_link
from tests.e2e.test_dashboard import login, submit
from tests.fixtures.submission import wait_for_task
from tests.manual.mock_robot_ws import Simulator, command_session

pytestmark = pytest.mark.integration


class RecordedRobot(Simulator):
    def __init__(self, name, place, duration):
        super().__init__(name, '模拟地图', [place], [], duration)
        self.commands, self.receipts = [], []

    def handle(self, frame, now):
        if frame.get('type') in {'command', 'cancel'}:
            self.commands.append(frame)
        reply = super().handle(frame, now)
        if reply and reply not in self.receipts:
            self.receipts.append(reply)
        return reply

    def finish(self, state, detail):
        reply = super().finish(state, detail)
        self.receipts.append(reply)
        return reply


async def wait_connected(app, binding):
    async with asyncio.timeout(15):
        while True:
            user = await app.state.identity.resolve_web('web')
            details = await app.state.resources.online_robot_details(user)
            if any(s.robot_binding_id == binding and s.waypoints and s.readiness.get('busy') is False for s in details):
                return
            await asyncio.sleep(0.01)


async def test_browser_a_running_b_finishes_then_list_stop_a(configuration, postgres_store, tmp_path):
    add_b(configuration)
    a, b = RecordedRobot('test-robot', 'A的前台', 60), RecordedRobot('robot-b', 'B的客厅', 3)
    workers = []
    async with platform(configuration, postgres_store, tmp_path, execute=True) as (app, base, web, prompts):
        workers.append(asyncio.create_task(command_session(base + '/api/voice/robot/ws', 'device', a)))
        try:
            await wait_connected(app, 'test-binding')
            async with async_playwright() as pw:
                browser = await pw.chromium.launch(channel='msedge')
                try:
                    page = await browser.new_page(viewport={'width': 1440, 'height': 1000})
                    errors = []
                    page.on('pageerror', lambda error: errors.append(str(error)))
                    await page.goto(base.replace('ws:', 'http:'))
                    await login(page, 'web')
                    await submit(page, '第一单：去 A 的前台')
                    await expect(page.locator('#results')).to_contain_text('机器人正在执行')
                    a_id = await page.locator('#task-id').text_content()
                    workers.append(asyncio.create_task(command_session(base + '/api/voice/robot/ws', 'device-b', b)))
                    await wait_connected(app, 'binding-b')
                    await page.locator('#sidebar-new').click()
                    await submit(page, '第二单：去 B 的客厅')
                    b_id = await page.locator('#task-id').text_content()
                    await expect(page.locator('#task-robots')).to_contain_text('计划机器人：binding-b')
                    await page.get_by_role('link', name='首页', exact=True).click()
                    try:
                        await expect(page.locator('#home-running')).to_contain_text('第一单：去 A 的前台')
                        await expect(page.locator('#home-running')).to_contain_text('第二单：去 B 的客厅')
                    except AssertionError:
                        diagnosis = {'a': (await web.get('/api/robot-agent/tasks/' + a_id)).json(),
                                     'b': (await web.get('/api/robot-agent/tasks/' + b_id)).json(),
                                     'a_commands': a.commands, 'a_receipts': a.receipts,
                                     'b_commands': b.commands, 'b_receipts': b.receipts}
                        (tmp_path / 'concurrent-flow-failure.json').write_text(
                            json.dumps(diagnosis, ensure_ascii=False, indent=2), encoding='utf-8')
                        raise
                    await page.screenshot(path=str(tmp_path / 'a-and-b-running.png'), full_page=True)
                    b_final = await wait_for_task(web, b_id, states={'SUCCEEDED'})
                    assert (await web.get('/api/robot-agent/tasks/' + a_id)).json()['state'] == 'RUNNING'
                    await page.get_by_role('link', name='任务', exact=True).click()
                    row = page.locator('#history [data-task-id="' + a_id + '"]')
                    await row.locator('.task-stop').click()
                    await expect(row).to_contain_text('已停止')
                    a_final = await wait_for_task(web, a_id, states={'CANCELLED'})
                    await expect(page.locator('#history [data-task-id="' + b_id + '"]')).to_contain_text('已完成')
                    await page.screenshot(path=str(tmp_path / 'a-stopped-b-completed.png'), full_page=True)
                    assert not errors
                    assert [c['type'] for c in a.commands] == ['command', 'cancel']
                    assert [c['type'] for c in b.commands] == ['command']
                    assert a.commands[0]['args'] == {'location': 'A的前台'}
                    assert b.commands[0]['args'] == {'location': 'B的客厅'}
                    assert a.receipts[-1]['state'] == 'failed' and b.receipts[-1]['state'] == 'succeeded'
                    assert await postgres_store.pool.fetchval('SELECT count(*) FROM ra_execution_guard') == 0
                    assert await postgres_store.pool.fetchval('SELECT count(*) FROM ra_commands') == 2
                    (tmp_path / 'browser-two-robot-flow.json').write_text(json.dumps({
                        'model_inputs': prompts, 'a_commands': a.commands, 'a_receipts': a.receipts,
                        'b_commands': b.commands, 'b_receipts': b.receipts, 'a_final': a_final,
                        'b_final': b_final, 'page_errors': errors}, ensure_ascii=False, indent=2), encoding='utf-8')
                finally:
                    await browser.close()
        finally:
            for worker in workers:
                worker.cancel()
            for worker in workers:
                with suppress(asyncio.CancelledError):
                    await worker


async def test_formal_slow_third_plan_on_a_does_not_hold_b_second_command(configuration, postgres_store, tmp_path):
    add_b(configuration)
    slow_entered, release = asyncio.Event(), asyncio.Event()
    async def model_hook(context):
        if context['goal'] == '第三单':
            slow_entered.set()
            await release.wait()
    evidence = {'commands': [], 'receipts': []}
    async with platform(configuration, postgres_store, tmp_path, execute=True, model_hook=model_hook, two_steps=True) as (app, base, web, prompts):
        async with robot_link(app, base) as a:
            async def create(text):
                reply = await web.post('/api/robot-agent/tasks', json={'text': text}, headers={'Idempotency-Key': text.encode().hex()})
                assert reply.status_code == 202
                return reply.json()['task_id']
            async def command(ws, robot):
                frame = json.loads(await asyncio.wait_for(ws.recv(), 4))
                assert frame['type'] == 'command'
                evidence['commands'].append({'robot': robot, **frame})
                return frame
            async def success(ws, frame):
                reply = {'type': 'cmd_status', 'cmd_id': frame['cmd_id'], 'state': 'succeeded'}
                evidence['receipts'].append(reply)
                await ws.send(json.dumps(reply))
            first = await create('第一单')
            a1 = await command(a, 'A')
            async with robot_link(app, base, name='robot-b', token='device-b', binding='binding-b') as b:
                second = await create('第二单')
                b1 = await command(b, 'B')
                third = await create('第三单')
                queued = await wait_for_task(web, third, states={'QUEUED'})
                assert not queued['plan_steps']
                await success(a, a1)
                await success(a, await command(a, 'A'))
                await asyncio.wait_for(slow_entered.wait(), 4)
                try:
                    await success(b, b1)
                    b2 = await command(b, 'B')
                    assert not release.is_set()
                    assert (await web.get('/api/robot-agent/tasks/' + third)).json()['state'] == 'PLANNING'
                    await success(b, b2)
                    await wait_for_task(web, second, states={'SUCCEEDED'})
                finally:
                    release.set()
                for _ in range(2):
                    await success(a, await command(a, 'A'))
                views = [await wait_for_task(web, identifier, states={'SUCCEEDED'}) for identifier in (first, second, third)]
                assert len(evidence['commands']) == len({c['cmd_id'] for c in evidence['commands']}) == 6
                assert [v['plan_robot_binding_id'] for v in views] == ['test-binding', 'binding-b', 'test-binding']
                assert await postgres_store.pool.fetchval('SELECT count(*) FROM ra_execution_guard') == 0
                evidence.update(model_inputs=prompts, final_tasks=views)
                (tmp_path / 'slow-third-plan-flow.json').write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding='utf-8')
