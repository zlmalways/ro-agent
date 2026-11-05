"""Disable through durable binding, observe production runtime, robot socket and browser."""
import asyncio
import json

from playwright.async_api import async_playwright, expect
import pytest

from tests.acceptance.test_scheduling import add_b, platform, robot_link
from tests.acceptance.test_restart_abandonment import task_until
from tests.e2e.test_dashboard import login

pytestmark=pytest.mark.integration


async def test_disabled_a_ends_b_continues_and_page_explains_unknown_result(configuration, postgres_store, tmp_path):
    add_b(configuration)
    async with platform(configuration,postgres_store,tmp_path,execute=True,two_steps=True) as (app,base,web,prompts):
        async with robot_link(app,base) as a:
            response=await web.post('/api/robot-agent/tasks',json={'text':'停用A测试：旧任务'},headers={'Idempotency-Key':'old-a'})
            a_id=response.json()['task_id']
            a_command=json.loads(await asyncio.wait_for(a.recv(),4))
            async with robot_link(app,base,name='robot-b',token='device-b',binding='binding-b') as b:
                response=await web.post('/api/robot-agent/tasks',json={'text':'B继续执行'},headers={'Idempotency-Key':'b'})
                b_id=response.json()['task_id']
                b_commands=[json.loads(await asyncio.wait_for(b.recv(),4))]
                await postgres_store.pool.execute("UPDATE ra_robot_bindings SET enabled=false,version=version+1 WHERE binding_id='test-binding'")
                ended=await task_until(web,a_id,lambda task:task['reason_code']=='ROBOT_DISABLED')
                assert ended['state']=='CANCELLED' and ended['result_summary'][0]['outcome']=='RESULT_UNKNOWN'
                assert ended['allocation_robot_binding_id'] is None
                with pytest.raises(TimeoutError):
                    await asyncio.wait_for(a.recv(),0.2)  # No automatic physical cancel or next step.
                for index in range(2):
                    await b.send(json.dumps(dict(type='cmd_status',cmd_id=b_commands[index]['cmd_id'],state='succeeded')))
                    if index==0:
                        b_commands.append(json.loads(await asyncio.wait_for(b.recv(),4)))
                b_final=await task_until(web,b_id,lambda task:task['state']=='SUCCEEDED')
                async with async_playwright() as pw:
                    browser=await pw.chromium.launch(channel='msedge')
                    try:
                        page=await browser.new_page(viewport={'width':1440,'height':1000})
                        errors=[]
                        page.on('pageerror',lambda error:errors.append(str(error)))
                        await page.goto(base.replace('ws:','http:'))
                        await login(page,'web')
                        await page.get_by_role('link',name='任务',exact=True).click()
                        row=page.locator('#history [data-task-id="'+a_id+'"]')
                        await expect(row).to_contain_text('任务已结束')
                        await expect(row).to_contain_text('机器人已停用')
                        await row.locator('.task-open').click()
                        await expect(page.locator('#status')).to_have_text('任务已结束')
                        await expect(page.locator('#reason')).to_contain_text('机器人已停用')
                        await expect(page.locator('#results')).to_contain_text('执行结果未知')
                        await expect(page.locator('#cancel')).to_be_hidden()
                        await page.screenshot(path=str(tmp_path/'disabled-desktop.png'),full_page=True)
                        await page.set_viewport_size({'width':390,'height':844})
                        await page.screenshot(path=str(tmp_path/'disabled-mobile.png'),full_page=True)
                        assert not errors
                    finally:
                        await browser.close()
                # Reenable and accept old evidence, but do not resume its second step.
                await postgres_store.pool.execute("UPDATE ra_robot_bindings SET enabled=true,version=version+1 WHERE binding_id='test-binding'")
                await a.send(json.dumps(dict(type='cmd_status',cmd_id=a_command['cmd_id'],state='succeeded')))
                final=await task_until(web,a_id,lambda task:task['result_summary'][0]['outcome']=='SUCCEEDED')
                assert (final['state'],final['reason_code'])==('CANCELLED','ROBOT_DISABLED')
                with pytest.raises(TimeoutError):
                    await asyncio.wait_for(a.recv(),0.2)
                assert await postgres_store.pool.fetchval('SELECT count(*) FROM ra_commands WHERE task_id=$1',a_id)==1
                assert await postgres_store.pool.fetchval('SELECT count(*) FROM ra_execution_guard')==0
                (tmp_path/'disabled-flow.json').write_text(json.dumps(dict(a_command=a_command,b_commands=b_commands,
                    after_disable=ended,a_final=final,b_final=b_final,model_inputs=prompts),ensure_ascii=False,indent=2),encoding='utf-8')
