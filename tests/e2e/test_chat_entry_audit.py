"""Audit the approved chat entry, navigation and observable stop states."""
# ruff: noqa: F811 -- pytest discovers the imported dashboard fixture.
import asyncio
from urllib.parse import urlsplit

from playwright.async_api import expect
import pytest

from tests.e2e.test_dashboard import dashboard, submit  # noqa: F401


pytestmark = pytest.mark.integration


async def test_first_entry_and_refresh_default_to_new_chat(dashboard, tmp_path):
    page, service, _ = dashboard
    base = page.url.split('#')[0]
    for fragment in ('', '#tasks', '#home', '#robots', '#settings', '#unknown'):
        await page.goto(base + fragment)
        # A fragment-only goto is same-document navigation, not a fresh app load.
        await page.reload()
        await expect(page.locator('#conversation')).to_be_visible()
        await expect(page.locator('#chat-empty')).to_be_visible()
        await expect(page.locator('#goal')).to_be_visible()
        await expect(page.locator('#send')).to_be_disabled()
        await expect(page.locator('#chat-access-note')).to_contain_text('连接工作台')
        assert urlsplit(page.url).fragment == 'chat'
        await expect(page.locator('#tasks')).to_be_hidden()
    for width in (1440, 390, 320):
        await page.set_viewport_size({'width':width,'height':900})
        assert await page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        await page.screenshot(path=str(tmp_path / f'entry-{width}.png'), full_page=True)
    assert await service.store.pool.fetchval('SELECT count(*) FROM ra_tasks') == 0


async def test_connect_chat_and_explicit_list_navigation(dashboard):
    page, _, _ = dashboard
    await page.get_by_role('link', name='设置', exact=True).click()
    await page.get_by_label('访问凭据').fill('first')
    await page.get_by_role('button', name='连接服务', exact=True).click()
    await expect(page.locator('#conversation')).to_be_visible()
    assert urlsplit(page.url).fragment == 'chat'
    await expect(page.locator('#chat-access-note')).to_be_hidden()
    await page.get_by_role('link', name='任务', exact=True).click()
    await expect(page.locator('#history')).to_be_visible()
    await expect(page.locator('#conversation')).to_be_hidden()
    await page.locator('#sidebar-new').click()
    assert urlsplit(page.url).fragment == 'chat'
    await expect(page.locator('#goal')).to_be_visible()
    await page.go_back()
    await expect(page.locator('#tasks')).to_be_visible()
    await page.reload()
    await expect(page.locator('#chat-empty')).to_be_visible()
    assert urlsplit(page.url).fragment == 'chat'


async def test_finished_conversation_has_bottom_new_task_action(dashboard):
    page, _, _ = dashboard
    await submit(page, '结束后再安排任务')
    await page.locator('#cancel').click()
    await expect(page.locator('#status')).to_have_text('已停止')
    await expect(page.locator('#followup-new')).to_be_visible()
    await page.locator('#followup-new').click()
    await expect(page.locator('#goal')).to_have_value('')
    await expect(page.locator('#chat-empty')).to_be_visible()
    await expect(page.locator('#task-detail')).to_be_hidden()


async def test_rejected_input_is_not_shown_as_an_active_task(dashboard):
    page, service, _ = dashboard
    service.max_text_chars = 3
    await page.locator('#sidebar-new').click()
    await page.get_by_label('任务指令').fill('这个请求超过长度限制')
    await page.locator('#send').click()
    await expect(page.locator('#status')).to_have_text('提交失败')
    await expect(page.locator('#composer-message')).to_contain_text('未受理')
    await expect(page.locator('#cancel')).to_be_hidden()
    await expect(page.locator('#followup-new')).to_be_visible()
    assert await service.store.pool.fetchval('SELECT count(*) FROM ra_tasks') == 0


async def test_open_execution_evidence_survives_status_polling(dashboard):
    page, service, _ = dashboard
    await submit(page, '查看机器人反馈依据')
    identifier = await page.locator('#task-id').text_content()
    await service.dispatch_next('test-deployment')
    details = page.locator('#results details').first
    await details.locator('summary').click()
    for _ in range(2):
        async with page.expect_response(lambda response: response.url.endswith('/tasks/' + identifier)
                                        and response.request.method == 'GET'):
            pass
    assert await details.evaluate('(element) => element.open')


async def test_failed_stop_stays_visible_in_chat_and_list(dashboard):
    page, _, _ = dashboard
    await submit(page, '停止失败后需要明确提示')
    identifier = await page.locator('#task-id').text_content()
    cancel_url = '**/api/robot-agent/tasks/' + identifier + '/cancel'
    await page.route(cancel_url, lambda route: route.abort())
    await page.locator('#cancel').click()
    await expect(page.locator('#error')).to_contain_text('停止尚未确认')
    async with page.expect_response(lambda response: response.url.endswith('/tasks/' + identifier)
                                    and response.request.method == 'GET'):
        pass
    await expect(page.locator('#stop-hint')).to_contain_text('停止尚未确认')
    await expect(page.locator('#cancel')).to_have_text('重试停止')
    await page.get_by_role('link', name='任务', exact=True).click()
    row = page.locator('#history [data-task-id="' + identifier + '"]')
    await expect(row).to_contain_text('停止尚未确认')
    await expect(row.locator('.task-stop')).to_have_text('重试停止')
    await page.unroute(cancel_url)
    await row.locator('.task-stop').click()
    await expect(row).to_contain_text('已停止')
    await expect(row).not_to_contain_text('停止尚未确认')


@pytest.mark.parametrize("feedback_first", [False, True])
async def test_recovered_poll_clears_obsolete_stop_error(dashboard, feedback_first):
    page, service, _ = dashboard
    await submit(page, '停止已受理但回复丢失')
    identifier = await page.locator('#task-id').text_content()
    release = asyncio.Event()

    async def hold_poll(route):
        await release.wait()
        await route.continue_()

    async def lose_cancel_reply(route):
        await route.fetch()
        if feedback_first:
            await release.wait()
        await route.abort()

    if not feedback_first:
        await page.route('**/api/robot-agent/tasks/' + identifier, hold_poll)
    await page.route('**/api/robot-agent/tasks/' + identifier + '/cancel', lose_cancel_reply)
    try:
        await page.locator('#cancel').click()
        if feedback_first:
            await expect(page.locator('#status')).to_have_text('已停止')
            async with page.expect_event('requestfailed', predicate=lambda request: request.url.endswith('/cancel')):
                release.set()
            async with page.expect_response(lambda response: response.url.endswith('/tasks/' + identifier)
                                            and response.request.method == 'GET'):
                pass
        else:
            await expect(page.locator('#error')).to_contain_text('停止尚未确认')
            release.set()
        await expect(page.locator('#status')).to_have_text('已停止')
        await expect(page.locator('#error')).to_be_empty()
        await expect(page.locator('#cancel')).to_be_hidden()
        assert await service.store.pool.fetchval(
            "SELECT count(*) FROM ra_task_events WHERE event_type='cancel_requested'") == 1
    finally:
        release.set()
