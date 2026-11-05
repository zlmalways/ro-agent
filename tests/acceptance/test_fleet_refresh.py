"""Step 26: hold real HTTP replies; never substitute list/detail success payloads."""
import asyncio
from contextlib import asynccontextmanager

from playwright.async_api import async_playwright, expect
import pytest

from tests.acceptance.test_scheduling import add_b, platform, robot_link
from tests.e2e.test_dashboard import login

pytestmark = pytest.mark.integration


@asynccontextmanager
async def fleet_page(configuration, store, tmp_path):
    add_b(configuration)
    configuration['users'].append(dict(customer_id='other',actor_id='other-user',roles=[],token_env='TOKEN_B'))
    async with platform(configuration,store,tmp_path) as (app,base,web,_), robot_link(app,base) as ws:
        async with async_playwright() as pw:
            browser=await pw.chromium.launch(channel='msedge')
            page=await browser.new_page()
            errors=[]
            page.on('pageerror',lambda e: errors.append(str(e)))
            # Only manual refreshes in these race tests; normal rendering/time remain live.
            await page.add_init_script("const interval=window.setInterval; window.setInterval=(f,t,...a)=>t===4000 ? 0 : interval(f,t,...a);")
            try:
                await page.goto(base.replace('ws:','http:'))
                await login(page,'web')
                await page.get_by_role('link',name='舰队',exact=True).click()
                await page.locator('[data-binding-id="test-binding"]').click()
                await expect(page.locator('#robot-fields')).to_contain_text('已就绪')
                yield app,base,web,ws,page
                assert not errors
            finally:
                await browser.close()


async def historical_pair(page):
    await expect(page.locator('#robot-fields')).to_contain_text('研发桌一')
    await expect(page.locator('#robot-fields')).not_to_contain_text('已就绪')
    await expect(page.locator('#robot-status')).to_contain_text('历史')
    await expect(page.locator('#robot-status')).to_contain_text('尚未确认')
    await expect(page.locator('[data-binding-id="binding-b"]')).not_to_contain_text('已停用')


@pytest.mark.parametrize('held',['list','detail'])
async def test_full_refresh_waits_for_both_before_publishing(configuration,postgres_store,tmp_path,held):
    async with fleet_page(configuration,postgres_store,tmp_path) as (_,_,_,_,page):
        await postgres_store.pool.execute("UPDATE ra_robot_bindings SET enabled=false WHERE binding_id='binding-b'")
        await postgres_store.pool.execute("UPDATE ra_robot_bindings SET observations=jsonb_set(observations,'{nav_server_ready,value}','false') WHERE binding_id='test-binding'")
        arrived,release=asyncio.Event(),asyncio.Event()
        async def hold(route):
            reply=await route.fetch()
            arrived.set()
            await release.wait()
            await route.fulfill(response=reply)
        await page.route('**/api/robot-agent/robots'+('' if held=='list' else '/test-binding'),hold)
        try:
            await page.locator('#refresh-robot').click()
            await asyncio.wait_for(arrived.wait(),5)
            await historical_pair(page)
            await page.screenshot(path=str(tmp_path/f'waiting-{held}.png'),full_page=True)
        finally:
            release.set()
            await page.unroute_all(behavior='wait')
        await expect(page.locator('[data-binding-id="binding-b"]')).to_contain_text('已停用')
        await expect(page.locator('#robot-fields')).to_contain_text('未就绪')
        await expect(page.locator('#robot-status')).to_contain_text('本次读取')


@pytest.mark.parametrize('failed',['list','detail'])
async def test_either_query_failure_keeps_both_old_and_retry_recovers(configuration,postgres_store,tmp_path,failed):
    async with fleet_page(configuration,postgres_store,tmp_path) as (_,_,_,_,page):
        await postgres_store.pool.execute("UPDATE ra_robot_bindings SET enabled=false WHERE binding_id='binding-b'")
        await page.route('**/api/robot-agent/robots'+('' if failed=='list' else '/test-binding'),lambda r:r.abort())
        await page.locator('#refresh-robot').click()
        await expect(page.locator('#robot-status')).to_contain_text('资料未更新')
        await historical_pair(page)
        await page.screenshot(path=str(tmp_path/f'failed-{failed}.png'),full_page=True)
        await page.unroute_all(behavior='wait')
        await page.locator('#refresh-robot').click()
        await expect(page.locator('[data-binding-id="binding-b"]')).to_contain_text('已停用')
        await expect(page.locator('#robot-fields')).to_contain_text('已就绪')
        await page.set_viewport_size(dict(width=390,height=844))
        await page.screenshot(path=str(tmp_path/'refreshed-mobile.png'),full_page=True)


async def test_click_only_reads_details_and_late_other_robot_cannot_overwrite(configuration,postgres_store,tmp_path):
    async with fleet_page(configuration,postgres_store,tmp_path) as (_,_,_,_,page):
        requests=[]
        page.on('request',lambda r:requests.append(r.url.split('/api/robot-agent')[-1]))
        arrived,release=asyncio.Event(),asyncio.Event()
        async def hold(route):
            reply=await route.fetch()
            arrived.set()
            await release.wait()
            await route.fulfill(response=reply)
        await page.route('**/api/robot-agent/robots/binding-b',hold)
        try:
            await page.locator('[data-binding-id="binding-b"]').click()
            await asyncio.wait_for(arrived.wait(),5)
            await historical_pair(page)
            await page.locator('[data-binding-id="test-binding"]').click()
            await expect(page.locator('#robot-fields')).to_contain_text('已就绪')
        finally:
            release.set()
            await page.unroute_all(behavior='wait')
        await expect(page.locator('#robot-fields')).to_contain_text('研发桌一')
        await expect(page.locator('[data-binding-id="test-binding"]')).to_have_attribute('aria-pressed','true')
        assert requests.count('/robots')==0
        assert requests.count('/robots/binding-b')==1
        assert requests.count('/robots/test-binding')==1


async def test_pending_reply_cannot_restore_previous_customer(configuration,postgres_store,tmp_path):
    async with fleet_page(configuration,postgres_store,tmp_path) as (_,_,_,_,page):
        arrived,release=asyncio.Event(),asyncio.Event()
        async def hold(route):
            reply=await route.fetch()
            arrived.set()
            await release.wait()
            await route.fulfill(response=reply)
        await page.route('**/api/robot-agent/robots/test-binding',hold)
        try:
            await page.locator('#refresh-robot').click()
            await asyncio.wait_for(arrived.wait(),5)
            await login(page,'device-b')
            await page.get_by_role('link',name='舰队',exact=True).click()
            await expect(page.locator('#robot-status')).to_contain_text('尚未登记')
        finally:
            release.set()
            await page.unroute_all(behavior='wait')
        await expect(page.locator('#robot-fields')).to_be_empty()
        await expect(page.locator('#robot-list')).to_be_empty()


async def test_reconnect_between_queries_does_not_mix_connection_generations(configuration,postgres_store,tmp_path):
    async with fleet_page(configuration,postgres_store,tmp_path) as (app,base,_,ws,page):
        arrived,release=asyncio.Event(),asyncio.Event()
        async def hold(route):
            reply=await route.fetch()
            arrived.set()
            await release.wait()
            await route.fulfill(response=reply)
        await page.route('**/api/robot-agent/robots',hold)
        try:
            await page.locator('#refresh-robot').click()
            await asyncio.wait_for(arrived.wait(),5)
            await ws.close()
            async with robot_link(app,base):
                release.set()
                await page.unroute_all(behavior='wait')
                await expect(page.locator('#robot-status')).to_contain_text('不一致')
                await expect(page.locator('#robot-fields')).not_to_contain_text('已就绪')
                await page.locator('#refresh-robot').click()
                await expect(page.locator('#robot-fields')).to_contain_text('已就绪')
        finally:
            release.set()
            await page.unroute_all(behavior='wait')


async def test_bad_list_cannot_be_overruled_by_newer_healthy_detail(configuration,postgres_store,tmp_path):
    async with fleet_page(configuration,postgres_store,tmp_path) as (_,_,_,_,page):
        await postgres_store.pool.execute("UPDATE ra_robot_bindings SET observations=jsonb_set(observations,'{nav_server_ready,value}','7') WHERE binding_id='test-binding'")
        arrived,release=asyncio.Event(),asyncio.Event()
        async def hold(route):
            reply=await route.fetch()
            arrived.set()
            await release.wait()
            await route.fulfill(response=reply)
        await page.route('**/api/robot-agent/robots',hold)
        try:
            await page.locator('#refresh-robot').click()
            await asyncio.wait_for(arrived.wait(),5)
            await postgres_store.pool.execute("UPDATE ra_robot_bindings SET observations=jsonb_set(observations,'{nav_server_ready,value}','true') WHERE binding_id='test-binding'")
        finally:
            release.set()
            await page.unroute_all(behavior='wait')
        await expect(page.locator('#robot-status')).to_contain_text('不一致')
        await expect(page.locator('#robot-fields')).not_to_contain_text('已就绪')
        await page.locator('#refresh-robot').click()
        await expect(page.locator('#robot-fields')).to_contain_text('已就绪')
