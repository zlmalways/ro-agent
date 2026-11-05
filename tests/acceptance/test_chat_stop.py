"""Real browser + production factory + PostgreSQL; local model and robot only."""
import asyncio
import json

from playwright.async_api import async_playwright, expect
import pytest

from tests.acceptance.test_scheduling import platform, robot_link
from tests.e2e.test_dashboard import login
from tests.fixtures.submission import wait_for_task
from tests.manual.mock_robot_ws import Simulator


pytestmark = pytest.mark.integration


@pytest.mark.parametrize('entry', ['chat', 'list'])
async def test_stop_while_submit_reply_and_model_are_both_pending(configuration, postgres_store, tmp_path, entry):
    entered, release_model, finished_model = asyncio.Event(), asyncio.Event(), asyncio.Event()
    held_reply, release_reply = asyncio.Event(), asyncio.Event()
    accepted = {}
    async with platform(configuration, postgres_store, tmp_path, execute=True) as (app, base, web, prompts):
        original = app.state.service.planner.plan

        async def slow(*args):
            entered.set()
            try:
                await release_model.wait()
            except asyncio.CancelledError:
                # Deliberately emulate a provider that still returns after local
                # cancellation. The persisted cancelled task must not revive.
                await release_model.wait()
            try:
                return await original(*args)
            finally:
                finished_model.set()

        app.state.service.planner.plan = slow
        async with robot_link(app, base) as ws, async_playwright() as pw:
            browser = await pw.chromium.launch(channel='msedge')
            try:
                page = await browser.new_page(viewport={'width':1440,'height':1000})
                errors, requests = [], []
                page.on('pageerror', lambda error: errors.append(str(error)))
                page.on('request', lambda request: requests.append(request.url) if request.url.endswith('/cancel') else None)
                await page.goto(base.replace('ws:', 'http:'))
                await login(page, 'web')

                async def hold(route):
                    if route.request.method != 'POST':
                        await route.continue_()
                        return
                    response = await route.fetch()
                    accepted.update(await response.json())
                    held_reply.set()
                    await release_reply.wait()
                    await route.fulfill(response=response)

                await page.route('**/api/robot-agent/tasks', hold)
                await page.locator('#sidebar-new').click()
                await page.get_by_label('任务指令').fill('请去前台')
                await page.locator('#send').click()
                await expect(page.locator('#task-goal')).to_have_text('请去前台')
                await expect(page.locator('#status')).to_have_text('正在处理')
                await expect(page.locator('#cancel')).to_be_enabled()
                await asyncio.wait_for(held_reply.wait(), 5)
                await asyncio.wait_for(entered.wait(), 5)
                assert accepted['state'] == 'RECEIVED'
                await page.screenshot(path=str(tmp_path/'submitted-before-reply.png'), full_page=True)
                if entry == 'chat':
                    # The task ID is still in the delayed reply. Remember stop intent.
                    await page.locator('#cancel').click()
                    await expect(page.locator('#status')).to_have_text('正在停止')
                    release_reply.set()
                else:
                    # The independent list stop must work while global writing is true.
                    await page.get_by_role('link', name='任务', exact=True).click()
                    row = page.locator('#history [data-task-id="' + accepted['task_id'] + '"]')
                    await expect(row).to_contain_text('正在生成计划')
                    await page.screenshot(path=str(tmp_path/'list-before-stop.png'), full_page=True)
                    await row.locator('.task-stop').click()
                    await expect(row).to_contain_text('已停止')
                    await row.locator('.task-open').click()
                    await expect(page.locator('#status')).to_have_text('已停止')
                    release_reply.set()
                await expect(page.locator('#status')).to_have_text('已停止')
                await expect(page.locator('#task-id')).to_have_text(accepted['task_id'])
                release_model.set()
                await asyncio.wait_for(finished_model.wait(), 5)
                final = await wait_for_task(web, accepted['task_id'], states={'CANCELLED'})
                with pytest.raises(TimeoutError):
                    await asyncio.wait_for(ws.recv(), 0.3)
                assert await postgres_store.pool.fetchval('SELECT count(*) FROM ra_plans') == 0
                assert await postgres_store.pool.fetchval('SELECT count(*) FROM ra_commands') == 0
                assert requests and not errors and final['plan_id'] is None
                await expect(page.locator('#task-goal')).to_have_text('请去前台')
                await expect(page.locator('#status')).to_have_text('已停止')
                await page.screenshot(path=str(tmp_path/'stopped-after-late-replies.png'), full_page=True)
                for width in (390, 320):
                    await page.set_viewport_size({'width':width,'height':844})
                    assert await page.evaluate('document.documentElement.scrollWidth <= innerWidth'), width
                await page.screenshot(path=str(tmp_path/'mobile-stopped.png'), full_page=True)
                (tmp_path/'evidence.json').write_text(json.dumps({'entry':entry,'accepted':accepted,'final':final,
                    'cancel_requests':requests,'model_requests':prompts,'commands':[],'page_errors':errors},
                    ensure_ascii=False, indent=2), encoding='utf-8')
            finally:
                release_model.set()
                release_reply.set()
                await browser.close()


async def test_running_stop_waits_for_simulator_receipt(configuration, postgres_store, tmp_path):
    simulator = Simulator('test-robot', '模拟地图', ['研发桌一'], [], 60)
    async with platform(configuration, postgres_store, tmp_path, execute=True) as (app, base, web, prompts):
        async with robot_link(app, base) as ws, async_playwright() as pw:
            browser = await pw.chromium.launch(channel='msedge')
            try:
                page = await browser.new_page(viewport={'width':1440,'height':1000})
                await page.goto(base.replace('ws:', 'http:'))
                await login(page, 'web')
                await page.locator('#sidebar-new').click()
                await page.get_by_label('任务指令').fill('请去研发桌一')
                await page.locator('#send').click()
                command = json.loads(await asyncio.wait_for(ws.recv(), 5))
                running = simulator.handle(command, asyncio.get_running_loop().time())
                assert running['state'] == 'running'
                await ws.send(json.dumps(running))
                await expect(page.locator('#results')).to_contain_text('机器人正在执行')
                await page.screenshot(path=str(tmp_path/'running.png'), full_page=True)
                identifier = await page.locator('#task-id').text_content()
                await page.locator('#cancel').click()
                cancel = json.loads(await asyncio.wait_for(ws.recv(), 5))
                assert cancel == {'type':'cancel','cmd_id':command['cmd_id']}
                await expect(page.locator('#status')).to_have_text('正在停止，等待机器人确认')
                assert (await web.get('/api/robot-agent/tasks/'+identifier)).json()['state'] == 'CANCEL_REQUESTED'
                await page.screenshot(path=str(tmp_path/'waiting-for-real-protocol-receipt.png'), full_page=True)
                # The simulator implements cancellation; this harness deliberately delays
                # delivering its terminal receipt so the waiting state is observable.
                receipt = simulator.handle(cancel, asyncio.get_running_loop().time())
                assert receipt['state'] == 'failed' and simulator.active is None
                await ws.send(json.dumps(receipt))
                await expect(page.locator('#status')).to_have_text('已停止')
                await expect(page.locator('#results')).to_contain_text('机器人报告失败')
                final = await wait_for_task(web, identifier, states={'CANCELLED'})
                assert await postgres_store.pool.fetchval('SELECT count(*) FROM ra_commands') == 1
                await page.screenshot(path=str(tmp_path/'stopped-with-receipt.png'), full_page=True)
                (tmp_path/'evidence.json').write_text(json.dumps({'model_requests':prompts,'command':command,
                    'running':running,'cancel':cancel,'receipt':receipt,'final':final},ensure_ascii=False,indent=2),encoding='utf-8')
            finally:
                await browser.close()
