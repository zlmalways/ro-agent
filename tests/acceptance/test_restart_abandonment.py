"""Step 17-18: actual app lifespans, PostgreSQL, HTTP, WS and browser."""
import asyncio
import json

import asyncpg
from fastapi import HTTPException
from playwright.async_api import async_playwright, expect
import pytest

from tests.acceptance.test_scheduling import platform, robot_link
from tests.e2e.test_dashboard import login, submit

pytestmark = pytest.mark.integration


async def task_until(web, identifier, predicate):
    async with asyncio.timeout(6):
        while True:
            task = (await web.get('/api/robot-agent/tasks/' + identifier)).json()
            if predicate(task):
                return task
            await asyncio.sleep(0.02)


async def show_old_task(page, base, identifier):
    await page.goto(base.replace('ws:', 'http:'))
    await login(page, 'web')
    await page.get_by_role('link', name='任务', exact=True).click()
    await page.locator('#history .task-open').filter(has_text='旧任务').click()
    await expect(page.locator('#task-id')).to_have_text(identifier)
    await expect(page.locator('#status')).to_have_text('任务已结束')
    await expect(page.locator('#reason')).to_contain_text('重启任务丢失')
    await expect(page.locator('#cancel')).to_be_hidden()


@pytest.mark.parametrize('execute_after', [False, True])
async def test_restart_before_serving_and_old_receipt_does_not_touch_new_task(configuration, postgres_store, tmp_path, execute_after):
    async with platform(configuration, postgres_store, tmp_path, execute=True, two_steps=True) as (app, base, web, _):
        async with robot_link(app, base) as ws:
            response = await web.post('/api/robot-agent/tasks', json={'text':'旧任务：先去桌边，再移动一次'},
                                      headers={'Idempotency-Key':'old'})
            identifier = response.json()['task_id']
            command = json.loads(await asyncio.wait_for(ws.recv(), 5))
            assert command['type'] == 'command'
            await task_until(web, identifier, lambda task: bool(task['result_summary']))
    async with platform(configuration, postgres_store, tmp_path, execute=execute_after) as (app, base, web, prompts):
        ended = (await web.get('/api/robot-agent/tasks/' + identifier)).json()
        assert ended['state'] == 'CANCELLED' and ended['reason_code'] == 'RESTART_TASK_LOST'
        assert ended['allocation_robot_binding_id'] is None
        assert ended['result_summary'][0]['outcome'] == 'RESULT_UNKNOWN'
        assert len(ended['plan_steps']) == 2 and not prompts
        # A current busy report blocks new work; startup never guesses robot idleness.
        async with robot_link(app, base, busy_cmd_id=command['cmd_id']) as ws:
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(ws.recv(), 0.2)
            response = await web.post('/api/robot-agent/tasks', json={'text':'新任务：去研发桌一'},
                                      headers={'Idempotency-Key':'new'})
            new_id = response.json()['task_id']
            waiting = await task_until(web, new_id, lambda task: task['state'] == 'QUEUED')
            assert not waiting['plan_id'] and not prompts
            await ws.send(json.dumps({'type':'readiness', 'busy':False, 'busy_cmd_id':''}))
            new = await task_until(web, new_id, lambda task: task['state'] in {'READY', 'RUNNING'})
            new_command = json.loads(await asyncio.wait_for(ws.recv(), 5)) if execute_after else None
            if not execute_after:
                with pytest.raises(TimeoutError):
                    await asyncio.wait_for(ws.recv(), 0.2)
            async with async_playwright() as pw:
                browser = await pw.chromium.launch(channel='msedge')
                try:
                    page = await browser.new_page()
                    errors = []
                    page.on('pageerror', lambda error: errors.append(str(error)))
                    await show_old_task(page, base, identifier)
                    await expect(page.locator('#results')).to_contain_text('执行结果未知')
                    await page.screenshot(path=str(tmp_path / 'restart-ended-desktop.png'), full_page=True)
                    await page.set_viewport_size({'width':390, 'height':844})
                    await page.screenshot(path=str(tmp_path / 'restart-ended-mobile.png'), full_page=True)
                    assert not errors
                finally:
                    await browser.close()
            receipt = {'type':'cmd_status', 'cmd_id':command['cmd_id'], 'state':'succeeded'}
            await ws.send(json.dumps(receipt))
            final_old = await task_until(web, identifier,
                lambda task: task['result_summary'][0]['outcome'] == 'SUCCEEDED')
            assert final_old['state'] == 'CANCELLED' and final_old['reason_code'] == 'RESTART_TASK_LOST'
            assert (await web.get('/api/robot-agent/tasks/' + new_id)).json()['allocation_robot_binding_id'] == 'test-binding'
            if execute_after:
                await ws.send(json.dumps({'type':'cmd_status', 'cmd_id':new_command['cmd_id'], 'state':'succeeded'}))
                new = await task_until(web, new_id, lambda task: task['state'] == 'SUCCEEDED')
            assert await postgres_store.pool.fetchval('SELECT count(*) FROM ra_commands WHERE task_id=$1', identifier) == 1
            assert len(prompts) == 1
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(ws.recv(), 0.2)
            (tmp_path / 'restart-flow.json').write_text(json.dumps({'dispatch_enabled_after':execute_after,
                'old_command':command, 'old_after_start':ended, 'late_receipt':receipt,
                'old_final':final_old, 'new_command':new_command, 'new_final':new, 'new_model_input':prompts},
                ensure_ascii=False, indent=2), encoding='utf-8')


@pytest.mark.parametrize('execute', [False, True])
async def test_formal_startup_failure_never_serves_and_can_retry(configuration, postgres_store, tmp_path, execute):
    async with platform(configuration, postgres_store, tmp_path) as (app, base, web, _):
        async with robot_link(app, base):
            response = await web.post('/api/robot-agent/tasks', json={'text':'旧任务：等执行'}, headers={'Idempotency-Key':'old'})
            identifier = response.json()['task_id']
            before = await task_until(web, identifier, lambda task: task['state'] == 'READY')
    await postgres_store.pool.execute("""
        CREATE FUNCTION reject_startup() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN IF NEW.reason_code='RESTART_TASK_LOST' THEN
          RAISE EXCEPTION 'startup rejected' USING ERRCODE='23514'; END IF; RETURN NEW; END $$;
        CREATE CONSTRAINT TRIGGER reject_startup AFTER UPDATE ON ra_tasks DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION reject_startup();
    """)
    with pytest.raises(asyncpg.CheckViolationError, match='startup rejected'):
        async with platform(configuration, postgres_store, tmp_path, execute=execute):
            pytest.fail('Failed startup must never expose HTTP or start scheduling')
    assert await postgres_store.pool.fetchval('SELECT state FROM ra_tasks WHERE task_id=$1', identifier) == before['state']
    assert await postgres_store.pool.fetchval('SELECT task_id FROM ra_execution_guard') == identifier
    await postgres_store.pool.execute('DROP TRIGGER reject_startup ON ra_tasks')
    # Same file/DB ownership can be acquired after failed startup cleanup.
    async with platform(configuration, postgres_store, tmp_path, execute=execute) as (app, base, web, _):
        ended = (await web.get('/api/robot-agent/tasks/' + identifier)).json()
        assert ended['state'] == 'CANCELLED' and ended['reason_code'] == 'RESTART_TASK_LOST'


async def test_real_model_http_error_visible_in_db_browser_and_retry(configuration, postgres_store, tmp_path):
    fails = True
    async def hook(context):
        if fails:
            raise HTTPException(503, 'intentional model outage')
    async with platform(configuration, postgres_store, tmp_path, execute=True, model_hook=hook) as (app, base, web, prompts):
        async with robot_link(app, base) as ws, async_playwright() as pw:
            browser = await pw.chromium.launch(channel='msedge')
            try:
                page = await browser.new_page()
                await page.goto(base.replace('ws:', 'http:'))
                await login(page, 'web')
                await submit(page, '模型故障测试：去研发桌一')
                identifier = await page.locator('#task-id').text_content()
                await expect(page.locator('#reason')).to_contain_text('模型服务暂时不可用')
                failed = (await web.get('/api/robot-agent/tasks/' + identifier)).json()
                assert failed['reason_code'] == 'PLANNER_UNAVAILABLE'
                assert await postgres_store.pool.fetchval('SELECT reason_code FROM ra_tasks WHERE task_id=$1', identifier) == 'PLANNER_UNAVAILABLE'
                assert await postgres_store.pool.fetchval('SELECT count(*) FROM ra_commands') == 0
                await page.screenshot(path=str(tmp_path / 'planning-error-desktop.png'), full_page=True)
                await page.set_viewport_size({'width':390, 'height':844})
                await page.screenshot(path=str(tmp_path / 'planning-error-mobile.png'), full_page=True)
                fails = False
                async with app.state.service.store.system_unit_of_work() as tx:
                    key = await tx.connection.fetchval("SELECT key FROM ra_request_keys WHERE result_ref=$1 AND operation='submit'", identifier)
                response = await web.post('/api/robot-agent/tasks', json={'text':'模型故障测试：去研发桌一'}, headers={'Idempotency-Key':key})
                assert response.json()['task_id'] == identifier
                command = json.loads(await asyncio.wait_for(ws.recv(), 5))
                await ws.send(json.dumps({'type':'cmd_status', 'cmd_id':command['cmd_id'], 'state':'succeeded'}))
                final = await task_until(web, identifier, lambda task: task['state'] == 'SUCCEEDED')
                assert len(prompts) == 2
                assert await postgres_store.pool.fetchval('SELECT count(*) FROM ra_tasks') == 1
                (tmp_path / 'planning-error-flow.json').write_text(json.dumps({'failure':failed, 'retry_final':final,
                    'command':command, 'model_calls':len(prompts)}, ensure_ascii=False, indent=2), encoding='utf-8')
            finally:
                await browser.close()


async def test_three_users_two_customers_three_robots_do_not_mix_tasks(configuration, postgres_store, tmp_path):
    from contextlib import AsyncExitStack
    from tests.acceptance.test_scheduling import add_b
    add_b(configuration)
    configuration['users'].extend([
        {'customer_id':'test-customer','actor_id':'user-b','roles':['user'],'token_env':'USER_B'},
        {'customer_id':'customer-c','actor_id':'user-c','roles':['user'],'token_env':'USER_C'}])
    configuration['robots'].append({'customer_id':'customer-c','binding_id':'binding-c','wire_robot_id':'robot-c',
        'allowed_actions':['move_to'],'token_env':'ROBOT_C'})
    configuration['policy']['actions']['customer-c'] = {'move_to':'allow'}
    env = {'USER_B':'web-b', 'USER_C':'web-c', 'ROBOT_C':'device-c'}
    async with platform(configuration, postgres_store, tmp_path, execute=True, environment_extra=env) as (app, base, web, prompts):
        async with AsyncExitStack() as stack:
            results = []
            for name, binding, token, user, customer in [('test-robot','test-binding','device','web','test-customer'),
                    ('robot-b','binding-b','device-b','web-b','test-customer'),
                    ('robot-c','binding-c','device-c','web-c','customer-c')]:
                ws = await stack.enter_async_context(robot_link(app, base, name=name, binding=binding, token=token, user_token=user))
                goal = user + ' 的移动任务'
                created = await web.post('/api/robot-agent/tasks', json={'text':goal},
                    headers={'Authorization':'Bearer '+user, 'Idempotency-Key':'same-key'})
                assert created.status_code == 202
                identifier = created.json()['task_id']
                command = json.loads(await asyncio.wait_for(ws.recv(), 5))
                row = await postgres_store.pool.fetchrow('SELECT customer_id,actor_id FROM ra_tasks WHERE task_id=$1', identifier)
                assert row['customer_id'] == customer
                results.append((ws, user, identifier, command, binding))
            assert len({row[2] for row in results}) == 3
            for ws, user, identifier, command, binding in results:
                for _, other_user, other_id, _, _ in results:
                    if identifier != other_id:
                        denied = await web.get('/api/robot-agent/tasks/' + other_id,
                                               headers={'Authorization':'Bearer '+user})
                        assert denied.status_code == 404
                # A robot cannot complete another robot's command, even inside one customer.
                wrong = next(row[3]['cmd_id'] for row in results if row[2] != identifier)
                await ws.send(json.dumps({'type':'cmd_status','cmd_id':wrong,'state':'succeeded'}))
            await asyncio.sleep(0.15)
            assert await postgres_store.pool.fetchval("SELECT count(*) FROM ra_tasks WHERE state='SUCCEEDED'") == 0
            finals = []
            for ws, user, identifier, command, binding in results:
                await ws.send(json.dumps({'type':'cmd_status','cmd_id':command['cmd_id'],'state':'succeeded'}))
                async with asyncio.timeout(5):
                    while True:
                        task = (await web.get('/api/robot-agent/tasks/' + identifier,
                                              headers={'Authorization':'Bearer '+user})).json()
                        if task['state'] == 'SUCCEEDED':
                            break
                        await asyncio.sleep(0.02)
                assert task['plan_robot_binding_id'] == binding
                assert task['result_summary'][0]['robot_binding_id'] == binding
                finals.append(task)
            assert await postgres_store.pool.fetchval('SELECT count(*) FROM ra_execution_guard') == 0
            assert len(prompts) == 3
            (tmp_path / 'multi-customer-flow.json').write_text(json.dumps({'model_inputs':prompts,
                'commands':[row[3] for row in results], 'final_tasks':finals}, ensure_ascii=False, indent=2), encoding='utf-8')
