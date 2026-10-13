import asyncio
import json

import httpx
from playwright.async_api import async_playwright
import pytest
from websockets.asyncio.client import connect

from robot_agent.adapters.dlv0 import Dlv0Robot
from robot_agent.adapters.identity import ConfiguredIdentity
from robot_agent.app import create_app
from robot_agent.application.input_service import InputService
from tests.integration.test_robot_ws import HELLO, TestWriteGate as WriteFixture, live_app


pytestmark = pytest.mark.integration


async def test_submit_clarify_approve_observe_result(persisted_service, tmp_path):
    service, _ = persisted_service
    robot = Dlv0Robot(service=service, write_gate=WriteFixture(), max_frame_bytes=8192)
    service.robot = robot
    service.policy.result = "require_confirmation"
    service.planner.value = {"outcome": "clarify", "question": "Which test table?", "required_slots": ["location"]}
    identity = ConfiguredIdentity(users={"test-user": {"customer_id": "test-customer", "actor_id": "test-user", "roles": ["user", "approver"]}},
                                  robots={"test-token": {"customer_id": "test-customer", "binding_id": "test-binding", "wire_robot_id": "test-robot"}})
    app = create_app(service=service, identity=identity, inputs=InputService(service, None, max_audio_bytes=1024))
    app.state.robot = robot
    async with live_app(app) as base:
        async with connect(base + "/api/voice/robot/ws?robot_id=test-robot", additional_headers={"x-robot-token": "test-token"}, proxy=None) as ws:
            await ws.recv()
            await ws.send(json.dumps(HELLO))
            await ws.recv()
            async with async_playwright() as playwright:
                browser = await playwright.chromium.launch(channel="msedge", headless=True)
                page = await browser.new_page()
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                await page.goto(base.replace("ws://", "http://") + "/")
                await page.get_by_label("访问凭据").fill("test-user")
                await page.get_by_label("任务指令").fill("test: fetch cup")
                await page.get_by_role("button", name="提交任务", exact=True).click()
                await page.get_by_text("Which test table?", exact=True).wait_for()
                service.planner.value = {"outcome": "plan", "steps": [
                    {"step_id": "s1", "action": "move_to", "args": {"location": "test-table"}, "depends_on": []},
                    {"step_id": "s2", "action": "vla_skill", "args": {"skill_id": "test-pick"}, "depends_on": ["s1"]},
                ]}
                await page.get_by_label("澄清回答").fill("test-table")
                await page.get_by_role("button", name="提交回答", exact=True).click()
                await page.get_by_role("button", name="批准计划", exact=True).wait_for()
                await page.get_by_role("button", name="批准计划", exact=True).click()
                await page.get_by_text("等待执行", exact=True).wait_for()
                for step in ("move_to", "vla_skill"):
                    await service.dispatch_next("test-deployment")
                    command = json.loads(await asyncio.wait_for(ws.recv(), 3))
                    assert command["skill"] == step
                    await ws.send(json.dumps({"type": "cmd_status", "cmd_id": command["cmd_id"], "state": "succeeded"}))
                    async with asyncio.timeout(3):
                        while await service.store.pool.fetchval("SELECT count(*) FROM ra_commands WHERE outcome='SUCCEEDED'") < (1 if step == "move_to" else 2):
                            await asyncio.sleep(0.01)
                await page.get_by_text("已完成", exact=True).wait_for()
                assert await page.get_by_text("机器人报告成功", exact=True).count() == 2
                assert errors == []
                await page.screenshot(path=str(tmp_path / "task-completed.png"), full_page=True)
                await browser.close()


async def test_event_replay_after_lost_notification(persisted_service):
    from robot_agent.domain.models import TaskInput, TrustedContext
    service, _ = persisted_service
    identity = ConfiguredIdentity(users={"test": {"customer_id": "test-customer", "actor_id": "test-user", "roles": []}}, robots={})
    view = await service.submit(TrustedContext("test-customer", "user", "test-user"), TaskInput("req", "web", "test", ""), "key")
    app = create_app(service=service, identity=identity)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
        first = await client.get(f"/api/robot-agent/tasks/{view.task_id}/events?after=0", headers={"Authorization": "Bearer test"})
        assert first.status_code == 200
        assert [event["seq"] for event in first.json()["events"]] == [1, 2]
        await service.dispatch_next("test-deployment")
        replay = await client.get(f"/api/robot-agent/tasks/{view.task_id}/events?after=2", headers={"Authorization": "Bearer test"})
        assert replay.status_code == 200
        assert [event["event_type"] for event in replay.json()["events"]] == ["command_prepared", "dispatching", "send_observed"]
