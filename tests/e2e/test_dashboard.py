"""Recovery cases reconstructed from the 2026-10-16 console acceptance record."""
import asyncio
import io
from types import SimpleNamespace
import wave

from playwright.async_api import async_playwright, expect
import pytest
import pytest_asyncio

from robot_agent.adapters.identity import ConfiguredIdentity
from robot_agent.app import create_app
from robot_agent.application.input_service import InputService
from tests.integration.test_robot_ws import live_app


pytestmark = pytest.mark.integration


class RecordingASR:
    def __init__(self):
        self.received = []

    async def transcribe(self, audio):
        self.received.append(audio)
        return SimpleNamespace(text="test: spoken goal", provider_ref="fixture:voice")


async def login(page, token="first"):
    await page.get_by_role("link", name="设置").click()
    await page.get_by_label("访问凭据").fill(token)
    await page.get_by_role("button", name="应用凭据").click()
    await expect(page.locator("#page-title")).to_have_text("首页")


async def submit(page, text):
    await page.get_by_role("button", name="描述你的目标").click()
    await page.get_by_label("任务指令").fill(text)
    await page.get_by_role("button", name="提交任务", exact=True).click()
    await expect(page.locator("#task-goal")).to_have_text(text)


@pytest_asyncio.fixture
async def dashboard(persisted_service):
    service, _ = persisted_service
    users = {key: {"customer_id": "test-customer", "actor_id": key, "roles": ["user", "approver"]}
             for key in ("first", "peer")}
    asr = RecordingASR()
    app = create_app(service=service, resources=service.resources, identity=ConfiguredIdentity(users=users, robots={}),
                     inputs=InputService(service, asr, max_audio_bytes=8_000_000))
    async with live_app(app) as address, async_playwright() as pw:
        browser = await pw.chromium.launch(channel="msedge", args=["--use-fake-device-for-media-stream"])
        context = await browser.new_context(permissions=["microphone"])
        page = await context.new_page()
        page.set_default_timeout(6000)
        try:
            await page.goto(address.replace("ws:", "http:"))
            await login(page)
            yield page, service, asr
        finally:
            await browser.close()


async def test_refresh_restores_history_only_for_same_identity(dashboard):
    page, _, _ = dashboard
    await submit(page, "private task")
    task_id = await page.locator("#task-id").inner_text()
    await page.reload()
    await expect(page.locator("#recent")).not_to_contain_text("private task")
    await login(page)
    await page.get_by_role("link", name="任务", exact=True).click()
    await page.locator("#history button").filter(has_text="private task").click()
    await expect(page.locator("#task-id")).to_have_text(task_id)
    await page.get_by_role("button", name="收起").click()
    await login(page, "peer")
    await expect(page.locator("#recent")).to_contain_text("没有已加载的任务")
    await expect(page.locator("#task-goal")).to_have_text("")


async def test_network_error_preserves_loaded_history(dashboard):
    page, _, _ = dashboard
    await submit(page, "keep on disconnect")
    await page.locator("#collapse").click()
    await page.get_by_role("link", name="任务", exact=True).click()
    await expect(page.locator("#history")).to_contain_text("keep on disconnect")
    await page.route("**/api/robot-agent/tasks", lambda route: route.abort())
    await page.locator("#refresh-tasks").click()
    await expect(page.locator("#notice")).to_contain_text("状态未更新")
    await expect(page.locator("#history")).to_contain_text("keep on disconnect")


async def test_lost_submit_body_allows_manual_replay_with_original_key(dashboard):
    page, service, _ = dashboard
    keys = []

    async def lose_first_response(route):
        if route.request.method != "POST":
            await route.continue_()
            return
        keys.append(route.request.headers["idempotency-key"])
        response = await route.fetch()
        if len(keys) == 1:
            await route.fulfill(status=response.status, content_type="application/json", body="{")
        else:
            await route.fulfill(response=response)

    await page.route("**/api/robot-agent/tasks", lose_first_response)
    await page.locator("#compose").click()
    await page.locator("#goal").fill("send once")
    await page.locator("#send").click()
    await expect(page.locator("#error")).to_contain_text("请求编号")
    assert len(keys) == 1 and keys[0] in await page.locator("#error").inner_text()
    await page.locator("#send").click()
    await expect(page.locator("#task-goal")).to_have_text("send once")
    assert keys == [keys[0], keys[0]]
    assert await service.store.pool.fetchval("SELECT count(*) FROM ra_tasks") == 1


async def test_late_poll_cannot_restore_cancelled_task_after_new_task(dashboard):
    page, _, _ = dashboard
    await submit(page, "old selection")
    task_id = await page.locator("#task-id").inner_text()
    started, release, finished = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def delayed_read(route):
        response = await route.fetch()
        started.set()
        await release.wait()
        await route.fulfill(response=response)
        finished.set()

    await page.route("**/api/robot-agent/tasks/" + task_id, delayed_read)
    try:
        await asyncio.wait_for(started.wait(), 5)
        await page.locator("#cancel").click()
        await expect(page.locator("#status")).to_have_text("任务已结束")
        await page.locator("#new-task").click()
    finally:
        release.set()
    await asyncio.wait_for(finished.wait(), 5)
    await expect(page.locator("#task-detail")).to_be_hidden()
    await expect(page.locator("#goal")).to_be_visible()


async def test_unknown_execution_is_visible_without_resubmitting(dashboard):
    page, service, _ = dashboard
    await submit(page, "uncertain execution")
    await service.dispatch_next("test-deployment")
    await service.recover("test-deployment")
    await expect(page.locator("#results")).to_contain_text("执行结果未知")
    await expect(page.locator("#status")).not_to_have_text("已完成")
    assert len(service.robot.sent) == 1


async def test_mobile_pages_fleet_and_keyboard(dashboard, tmp_path):
    page, _, _ = dashboard
    await page.set_viewport_size({"width": 390, "height": 844})
    await page.get_by_role("link", name="舰队").click()
    await expect(page.locator("#robot-fields")).to_contain_text("test-binding")
    await expect(page.locator("#robot-fields")).to_contain_text("test-table")
    await expect(page.locator("#robot-fields")).to_contain_text("test-pick")
    await page.keyboard.press("Control+k")
    await expect(page.get_by_role("dialog")).to_be_visible()
    assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    await page.keyboard.press("Escape")
    await expect(page.get_by_role("dialog")).to_be_hidden()
    await page.screenshot(path=str(tmp_path / "fleet-mobile.png"))


async def test_recording_response_lost_can_replay_same_wav_without_recording_again(dashboard):
    page, service, asr = dashboard
    keys = []

    async def lose_first_ack(route):
        keys.append(route.request.headers["idempotency-key"])
        response = await route.fetch()
        if len(keys) == 1:
            await route.fulfill(status=response.status, content_type="application/json", body="{")
        else:
            await route.fulfill(response=response)

    await page.route("**/api/robot-agent/tasks/audio", lose_first_ack)
    await page.locator("#dock").click()
    await page.get_by_text("语音输入", exact=True).click()
    await page.locator("#record").click()
    await expect(page.locator("#record-status")).to_have_text("正在录音")
    await page.wait_for_timeout(400)
    await page.locator("#record").click()
    await expect(page.locator("#error")).to_contain_text("请求编号")
    await page.locator("#audio-submit").click()
    await expect(page.locator("#task-goal")).to_have_text("test: spoken goal")
    assert keys == [keys[0], keys[0]]
    assert len(asr.received) == 1
    assert await service.store.pool.fetchval("SELECT count(*) FROM ra_tasks") == 1


@pytest.mark.parametrize("source", ["file", "microphone"])
async def test_wav_inputs_reach_task_service(dashboard, source):
    page, service, asr = dashboard
    await page.locator("#compose").click()
    await page.get_by_text("语音输入", exact=True).click()
    if source == "file":
        data = io.BytesIO()
        with wave.open(data, "wb") as wav:
            wav.setparams((1, 2, 16000, 0, "NONE", ""))
            wav.writeframes(b"\0\0" * 1600)
        await page.locator("#audio").set_input_files({"name": "test.wav", "mimeType": "audio/wav",
                                                       "buffer": data.getvalue()})
        await page.locator("#audio-submit").click()
    else:
        await page.get_by_role("button", name="开始录音").click()
        await expect(page.locator("#record-status")).to_have_text("正在录音")
        # Wait for an actual browser media sample, not just the button transition.
        await page.wait_for_timeout(400)
        await page.get_by_role("button", name="结束并提交录音").click()
    await expect(page.locator("#task-goal")).to_have_text("test: spoken goal")
    assert len(asr.received) == 1
    with wave.open(io.BytesIO(asr.received[0])) as wav:
        assert (wav.getframerate(), wav.getnchannels(), wav.getsampwidth()) == (16000, 1, 2)
        assert wav.getnframes() > 0
    assert await service.store.pool.fetchval("SELECT count(*) FROM ra_tasks") == 1
