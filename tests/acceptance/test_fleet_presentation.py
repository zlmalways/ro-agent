"""Segment 3: actual factory/HTTP/WS/browser against an isolated PostgreSQL schema."""

import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import os

import httpx
from playwright.async_api import async_playwright, expect
import pytest
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

from robot_agent.deployment import build_app
from tests.acceptance.test_scheduling import add_b, platform, robot_link
from tests.e2e.test_dashboard import login
from tests.fixtures.submission import wait_for_task
from tests.integration.test_robot_ws import live_app

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("all_missing", [False, True])
async def test_missing_credentials_and_restart_do_not_clear_other_restrictions(
    configuration, postgres_store, all_missing
):
    add_b(configuration)
    configuration["database"]["schema"] = await postgres_store.pool.fetchval("SELECT current_schema()")
    env = {"TEST_DSN": os.environ["RO_AGENT_TEST_DSN"], "TEST_USER_TOKEN": "web"}
    if not all_missing:
        env["TEST_ROBOT_TOKEN"] = "device"
    app = build_app(configuration, environment=env)
    async with app.router.lifespan_context(app), live_app(app) as base:
        async with httpx.AsyncClient(
            base_url=base.replace("ws:", "http:"), trust_env=False, headers={"Authorization": "Bearer web"}
        ) as web:
            rows = (await web.get("/api/robot-agent/robots")).json()
            assert sum("CREDENTIAL_MISSING" in r["issues"] for r in rows) == (2 if all_missing else 1)
            assert all("token_env" not in r and "credential_ref" not in r for r in rows)
            async with connect(
                base + "/api/voice/robot/ws?robot_id=robot-b",
                additional_headers={"x-robot-token": "missing"},
                proxy=None,
            ) as ws:
                with pytest.raises(ConnectionClosed) as closed:
                    await ws.recv()
                assert closed.value.rcvd.code == (1013 if all_missing else 4401)
            assert (await web.get("/health")).status_code == 200
            if all_missing:
                async with async_playwright() as pw:
                    browser = await pw.chromium.launch(channel="msedge")
                    try:
                        page = await browser.new_page()
                        await page.goto(base.replace("ws:", "http:"))
                        await login(page, "web")
                        await page.get_by_role("link", name="舰队", exact=True).click()
                        await expect(page.locator("#robot-fields")).to_contain_text("平台未配置机器人连接凭据")
                        await expect(page.locator("#robot-fields")).not_to_contain_text("已就绪")
                    finally:
                        await browser.close()
        await postgres_store.pool.execute(
            "UPDATE ra_robot_bindings SET enabled=false,isolation_reason='EVIDENCE_CONFLICT',observations='{\"brain_skills\":7}' WHERE binding_id='binding-b'"
        )
    # A genuine factory restart with the repaired secret: no database flags are cleared.
    app = build_app(configuration, environment={**env, "TEST_ROBOT_TOKEN": "device", "TOKEN_B": "device-b"})
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app),
            base_url="http://test",
            headers={"Authorization": "Bearer web"},
        ) as web:
            detail = (await web.get("/api/robot-agent/robots/binding-b")).json()
            assert set(detail["robot"]["issues"]) == {"DISABLED", "ISOLATED", "REPORT_INVALID", "OFFLINE"}
            assert detail["snapshot"] is None


async def test_http_robot_scope_and_resource_edit_are_explicit(configuration, postgres_store, tmp_path):
    add_b(configuration)
    configuration["users"][0]["roles"].append("maintainer")
    foreign = deepcopy(configuration["robots"][0])
    foreign.update(
        customer_id="other", binding_id="foreign-binding", wire_robot_id="foreign", token_env="FOREIGN"
    )
    configuration["robots"].append(foreign)
    async with platform(configuration, postgres_store, tmp_path) as (_, _, web, _):
        assert {r["binding_id"] for r in (await web.get("/api/robot-agent/robots")).json()} == {
            "test-binding",
            "binding-b",
        }
        for path in ["/robots/foreign-binding", "/resources/environment?binding_id=foreign-binding"]:
            assert (await web.get("/api/robot-agent" + path)).status_code == 403
        body = {"entries": {"desk": "A桌"}, "base_version": 0, "source_refs": ["test:survey"]}
        url = "/api/robot-agent/resources/environment/versions"
        assert (await web.post(url + "?binding_id=test-binding", json=body)).status_code == 201
        assert (await web.get("/api/robot-agent/resources/environment?binding_id=binding-b")).json() is None
        assert (await web.post(url + "?binding_id=foreign-binding", json=body)).status_code == 403
        assert (await web.get("/api/robot-agent/resources/environment")).status_code == 503


@pytest.mark.parametrize("kind", ["hello", "readiness"])
async def test_invalid_wire_report_only_closes_bad_robot(configuration, postgres_store, tmp_path, kind):
    add_b(configuration)
    async with (
        platform(configuration, postgres_store, tmp_path) as (app, base, web, prompts),
        robot_link(app, base),
    ):
        async with connect(
            base + "/api/voice/robot/ws?robot_id=robot-b",
            additional_headers={"x-robot-token": "device-b"},
            proxy=None,
        ) as b:
            await b.recv()
            generation = await postgres_store.pool.fetchval(
                "SELECT connection_generation FROM ra_robot_bindings WHERE binding_id='binding-b'"
            )
            if kind == "readiness":
                await b.send(
                    json.dumps(
                        {
                            "type": "hello",
                            "robot_id": "robot-b",
                            "skills": ["move_to"],
                            "nav_server_ready": True,
                        }
                    )
                )
                await b.recv()
                before = json.loads(
                    await postgres_store.pool.fetchval(
                        "SELECT observations FROM ra_robot_bindings WHERE binding_id='binding-b'"
                    )
                )
            await b.send(
                json.dumps(
                    {"type": kind, "robot_id": "robot-b", "brain_skills": 7, "nav_server_ready": False}
                )
            )
            with pytest.raises(ConnectionClosed) as closed:
                await b.recv()
            assert closed.value.rcvd.code == 1003
            stored = await postgres_store.pool.fetchrow(
                "SELECT * FROM ra_robot_bindings WHERE binding_id='binding-b'"
            )
            assert stored["enabled"] and not stored["isolation_reason"]
            if kind == "hello":
                assert stored["connection_generation"] == generation
            else:
                assert json.loads(stored["observations"])["nav_server_ready"] == before["nav_server_ready"]
            assert (await web.get("/api/robot-agent/robots/test-binding")).json()["robot"]["connected"]
            submitted = await web.post(
                "/api/robot-agent/tasks", json={"text": "去研发桌一"}, headers={"Idempotency-Key": "A-only"}
            )
            view = await wait_for_task(web, submitted.json()["task_id"])
            assert view["state"] == "READY" and view["plan_robot_binding_id"] == "test-binding"
            assert len(prompts) == 1
            assert await postgres_store.pool.fetchval("SELECT count(*) FROM ra_commands") == 0


async def test_browser_robot_switch_late_reply_failure_and_history(configuration, postgres_store, tmp_path):
    add_b(configuration)
    configuration["users"].append(
        {"customer_id": "other", "actor_id": "other-user", "roles": [], "token_env": "TOKEN_B"}
    )
    async with (
        platform(configuration, postgres_store, tmp_path) as (app, base, web, _),
        robot_link(app, base),
    ):
        async with robot_link(app, base, name="robot-b", token="device-b", binding="binding-b"):
            pass  # B's report remains as history after disconnect.
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(channel="msedge")
            try:
                page = await browser.new_page()
                errors = []
                page.on("pageerror", lambda e: errors.append(str(e)))
                await page.goto(base.replace("ws:", "http:"))
                await login(page, "web")
                await page.get_by_role("link", name="舰队", exact=True).click()
                await page.locator('[data-binding-id="test-binding"]').click()
                await expect(page.locator("#robot-fields")).to_contain_text("研发桌一")
                await expect(page.locator("#robot-fields")).to_contain_text("已就绪")
                delayed = asyncio.Event()
                release = asyncio.Event()

                async def late_b(route):
                    reply = await route.fetch()
                    delayed.set()
                    await release.wait()
                    await route.fulfill(response=reply)

                await page.route("**/api/robot-agent/robots/binding-b", late_b)
                await page.locator('[data-binding-id="binding-b"]').click()
                await asyncio.wait_for(delayed.wait(), 5)
                await expect(page.locator("#robot-fields")).not_to_be_empty()
                await expect(page.locator("#robot-fields")).not_to_contain_text("已就绪")
                await expect(page.locator("#robot-status")).to_contain_text("历史")
                await page.locator('[data-binding-id="test-binding"]').click()
                await expect(page.locator("#robot-fields")).to_contain_text("研发桌一")
                release.set()
                await page.unroute_all(behavior="wait")
                await expect(page.locator("#robot-fields")).not_to_contain_text("B的地点")
                await page.locator('[data-binding-id="binding-b"]').click()
                await expect(page.locator("#robot-fields")).to_contain_text("B的地点")
                await expect(page.locator("#robot-fields")).to_contain_text("历史上报航点")
                await expect(page.locator("#robot-fields")).not_to_contain_text("已就绪")
                await page.screenshot(path=str(tmp_path / "fleet-offline-history.png"), full_page=True)
                await postgres_store.pool.execute("UPDATE ra_robot_bindings SET observations=jsonb_set(observations,'{nav_server_ready,value}','7') WHERE binding_id='binding-b'")
                await page.locator("#refresh-robot").click()
                await expect(page.locator("#robot-fields")).to_contain_text("上报资料有误")
                await expect(page.locator("#robot-fields")).not_to_contain_text("已就绪")
                # A recovered list is not enough: until details return, no stale ready display.
                await page.route("**/api/robot-agent/robots/test-binding", lambda route: route.abort())
                await page.locator('[data-binding-id="test-binding"]').click()
                await expect(page.locator("#robot-status")).to_contain_text("资料未更新")
                await expect(page.locator("#robot-fields")).not_to_be_empty()
                await expect(page.locator("#robot-fields")).not_to_contain_text("已就绪")
                await expect(page.locator("#robot-status")).to_contain_text("历史")
                await page.unroute_all(behavior="wait")
                received = asyncio.Event()
                recover = asyncio.Event()

                async def delay_a(route):
                    received.set()
                    await recover.wait()
                    await route.continue_()

                await page.route("**/api/robot-agent/robots/test-binding", delay_a)
                await page.locator("#refresh-robot").click()
                await asyncio.wait_for(received.wait(), 5)
                await expect(page.locator("#robot-fields")).not_to_be_empty()
                await expect(page.locator("#robot-fields")).not_to_contain_text("已就绪")
                await expect(page.locator("#robot-status")).to_contain_text("历史")
                await expect(page.locator("#robot-status")).to_contain_text("尚未确认")
                recover.set()
                await page.unroute_all(behavior="wait")
                await expect(page.locator("#robot-fields")).to_contain_text("已就绪")
                # Expired report is unknown, and old names are explicitly history.
                stale = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
                await postgres_store.pool.execute(
                    "UPDATE ra_robot_bindings SET observations=jsonb_set(observations,'{nav_server_ready,received_at}',to_jsonb($1::text)) WHERE binding_id='test-binding'",
                    stale,
                )
                await page.locator("#refresh-robot").click()
                await expect(page.locator("#robot-fields")).not_to_contain_text("已就绪")
                await expect(page.locator("#robot-fields")).to_contain_text("尚未确认")
                # Changing customer clears both robot list and detail.
                await login(page, "device-b")
                await page.get_by_role("link", name="舰队", exact=True).click()
                await expect(page.locator("#robot-list")).to_be_empty()
                await expect(page.locator("#robot-status")).to_contain_text("尚未登记")
                assert not errors
            finally:
                await browser.close()


async def test_browser_task_target_does_not_follow_viewed_robot(configuration, postgres_store, tmp_path):
    add_b(configuration)
    async with (
        platform(configuration, postgres_store, tmp_path, execute=True) as (app, base, web, _),
        robot_link(app, base) as ws,
    ):
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(channel="msedge")
            try:
                page = await browser.new_page()
                await page.goto(base.replace("ws:", "http:"))
                await login(page, "web")
                await page.get_by_role("link", name="舰队", exact=True).click()
                await page.locator('[data-binding-id="binding-b"]').click()
                await expect(page.locator("#robot-fields")).to_contain_text("binding-b")
                await page.locator("#sidebar-new").click()
                await page.locator("#goal").fill("去研发桌一")
                await page.locator("#send").click()
                command = json.loads(await asyncio.wait_for(ws.recv(), 5))
                await expect(page.locator("#task-robots")).to_contain_text("当前分配：test-binding")
                await expect(page.locator("#task-robots")).to_contain_text("计划机器人：test-binding")
                await expect(page.locator("#results")).to_contain_text("指令目标：test-binding")
                await expect(page.locator("#results")).to_contain_text("已发送，执行结果看回执")
                await expect(page.locator("#results")).not_to_contain_text("机器人报告成功")
                await page.screenshot(path=str(tmp_path / "task-target-sent.png"), full_page=True)
                await ws.send(
                    json.dumps({"type": "cmd_status", "cmd_id": command["cmd_id"], "state": "succeeded"})
                )
                await expect(page.locator("#status")).to_have_text("已完成")
                await expect(page.locator("#task-robots")).to_contain_text("当前未占用机器人")
                await expect(page.locator("#task-robots")).to_contain_text("计划机器人：test-binding")
                await page.get_by_role("link", name="任务", exact=True).click()
                await expect(page.locator("#history")).to_contain_text("当前未占用机器人")
                await expect(page.locator("#history")).to_contain_text("计划机器人：test-binding")
            finally:
                await browser.close()
