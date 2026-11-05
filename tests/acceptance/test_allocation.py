"""C1 second segment: production factory/DB/HTTP/WS/runtime, local model and robot."""
import asyncio
import json

from playwright.async_api import async_playwright, expect
import pytest

from tests.acceptance.test_scheduling import platform, robot_link
from tests.e2e.test_dashboard import login, submit
from tests.fixtures.submission import wait_for_task


pytestmark = pytest.mark.integration


async def test_web_queue_plans_only_after_first_robot_receipt(configuration, postgres_store, tmp_path):
    async with platform(configuration, postgres_store, tmp_path, execute=True) as (app, base, web, prompts):
        async with robot_link(app, base) as ws, async_playwright() as pw:
            browser = await pw.chromium.launch(channel='msedge')
            try:
                page = await browser.new_page()
                errors = []
                page.on('pageerror', lambda error: errors.append(str(error)))
                await page.goto(base.replace('ws:', 'http:'))
                await login(page, 'web')
                await submit(page, '第一单：去研发桌一')
                first = await page.locator('#task-id').text_content()
                command1 = json.loads(await asyncio.wait_for(ws.recv(), 5))
                await page.locator('#new-task').click()
                await submit(page, '第二单：再去研发桌一')
                second = await page.locator('#task-id').text_content()
                await expect(page.locator('#status')).to_have_text('正在排队')
                queued = (await web.get('/api/robot-agent/tasks/' + second)).json()
                assert queued['state'] == 'QUEUED' and not queued['plan_id'] and not queued['plan_steps']
                assert len(prompts) == 1
                assert await postgres_store.pool.fetchval('SELECT count(*) FROM ra_plans WHERE task_id=$1', second) == 0
                assert await postgres_store.pool.fetchval('SELECT count(*) FROM ra_commands') == 1
                await page.locator('#collapse').click()
                await expect(page.locator('#home-running')).to_contain_text('第一单：去研发桌一')
                await expect(page.locator('#home-queue')).to_contain_text('第二单：再去研发桌一')
                await page.screenshot(path=str(tmp_path / 'queued-before-planning.png'), full_page=True)
                receipt1 = {'type':'cmd_status', 'cmd_id':command1['cmd_id'], 'state':'succeeded'}
                await ws.send(json.dumps(receipt1))
                command2 = json.loads(await asyncio.wait_for(ws.recv(), 5))
                assert command1['type'] == command2['type'] == 'command'
                assert command1['cmd_id'] != command2['cmd_id']
                assert command1['args'] == command2['args'] == {'location':'研发桌一'}
                assert [p['goal'] for p in prompts] == ['第一单：去研发桌一', '第二单：再去研发桌一']
                receipt2 = {'type':'cmd_status', 'cmd_id':command2['cmd_id'], 'state':'succeeded'}
                await ws.send(json.dumps(receipt2))
                async with asyncio.timeout(5):
                    while True:
                        views = [(await web.get('/api/robot-agent/tasks/' + tid)).json() for tid in (first, second)]
                        if all(view['state'] == 'SUCCEEDED' for view in views):
                            break
                        await asyncio.sleep(0.01)
                assert await postgres_store.pool.fetchval('SELECT count(*) FROM ra_commands') == 2
                assert await postgres_store.pool.fetchval('SELECT task_id FROM ra_execution_guard') is None
                assert not errors
                await expect(page.locator('#home-running')).not_to_contain_text('第一单：去研发桌一')
                await expect(page.locator('#home-queue')).not_to_contain_text('第二单：再去研发桌一')
                await page.screenshot(path=str(tmp_path / 'both-completed.png'), full_page=True)
                (tmp_path / 'two-task-flow.json').write_text(json.dumps({'queued_task':queued, 'model_requests':prompts,
                    'commands':[command1,command2], 'receipts':[receipt1,receipt2], 'final_tasks':views},
                    ensure_ascii=False, indent=2), encoding='utf-8')
            finally:
                await browser.close()


async def test_execution_disabled_still_plans_and_advances_queue_without_sending(configuration, postgres_store, tmp_path):
    async with platform(configuration, postgres_store, tmp_path) as (app, base, web, prompts):
        async with robot_link(app, base) as ws:
            async def create(key):
                response = await web.post('/api/robot-agent/tasks', json={'text':key}, headers={'Idempotency-Key':key})
                assert response.status_code == 202
                return await wait_for_task(web, response.json()['task_id'])
            first, second = await create('first'), await create('second')
            assert first['state'] == 'READY' and first['plan_id']
            assert second['state'] == 'QUEUED' and not second['plan_id'] and len(prompts) == 1
            cancelled = await web.post('/api/robot-agent/tasks/' + first['task_id'] + '/cancel',
                                      json={'expected_version':first['version']}, headers={'Idempotency-Key':'cancel'})
            assert cancelled.json()['state'] == 'CANCELLED'
            async with asyncio.timeout(5):
                while (await web.get('/api/robot-agent/tasks/' + second['task_id'])).json()['state'] != 'READY':
                    await asyncio.sleep(0.01)
            assert len(prompts) == 2
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(ws.recv(), 0.3)
            assert await postgres_store.pool.fetchval('SELECT count(*) FROM ra_commands') == 0


async def test_database_owner_loss_then_new_app_ends_old_task_and_accepts_new(configuration, postgres_store, tmp_path):
    entered, resume = asyncio.Event(), asyncio.Event()
    async with platform(configuration, postgres_store, tmp_path, execute=True) as (app, base, web, _):
        async with robot_link(app, base) as ws:
            original = app.state.service.planner.plan
            async def slow(*args):
                entered.set()
                await resume.wait()
                return await original(*args)
            app.state.service.planner.plan = slow
            request = asyncio.create_task(web.post('/api/robot-agent/tasks', json={'text':'重启后去研发桌一'},
                                                   headers={'Idempotency-Key':'restart-before-plan'}))
            try:
                await asyncio.wait_for(entered.wait(), 5)
                owner = app.state.runtime.owner
                pid = owner.connection.get_server_pid()
                assert await postgres_store.pool.fetchval('SELECT pg_terminate_backend($1)', pid)
                async with asyncio.timeout(5):
                    while await owner.healthy():
                        await asyncio.sleep(0.01)
                resume.set()
                response = await request
                assert response.status_code == 202
                identifier = response.json()['task_id']
                final = await wait_for_task(web, identifier)
                assert final['reason_code'] == 'OWNERSHIP_UNAVAILABLE'
                assert await postgres_store.pool.fetchval('SELECT count(*) FROM ra_commands') == 0
                with pytest.raises(TimeoutError):
                    await asyncio.wait_for(ws.recv(), 0.1)
            finally:
                resume.set()
                await request
    async with platform(configuration, postgres_store, tmp_path, execute=True) as (app, base, web, prompts):
        ended = (await web.get('/api/robot-agent/tasks/' + identifier)).json()
        assert ended['state'] == 'CANCELLED' and ended['reason_code'] == 'RESTART_TASK_LOST'
        assert not prompts and not ended['plan_id']
        async with robot_link(app, base) as ws:
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(ws.recv(), 0.2)
            created = await web.post('/api/robot-agent/tasks', json={'text':'新任务：去研发桌一'},
                                     headers={'Idempotency-Key':'after-restart'})
            new_id = created.json()['task_id']
            assert new_id != identifier
            command = json.loads(await asyncio.wait_for(ws.recv(), 5))
            assert command['skill'] == 'move_to' and len(prompts) == 1
            await ws.send(json.dumps({'type':'cmd_status','cmd_id':command['cmd_id'],'state':'succeeded'}))
            async with asyncio.timeout(5):
                while (await web.get('/api/robot-agent/tasks/' + new_id)).json()['state'] != 'SUCCEEDED':
                    await asyncio.sleep(0.01)
            assert (await web.get('/api/robot-agent/tasks/' + identifier)).json()['state'] == 'CANCELLED'
            assert await postgres_store.pool.fetchval('SELECT count(*) FROM ra_commands') == 1
