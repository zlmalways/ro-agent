"""Independent reconstruction of the missing Mac hello-only formal/browser scenario.

This is a new acceptance test, not recovered Mac source. It reuses the
production-factory harness and the repository's existing test fixtures.
Only a local *_test PostgreSQL database is accepted by the reused fixture.
"""

import asyncio
import json

from playwright.async_api import async_playwright, expect
import pytest
from websockets.asyncio.client import connect

from tests.acceptance.test_scheduling import platform
from tests.e2e.test_dashboard import login, submit


pytestmark = pytest.mark.integration


async def test_hello_only_formal_http_ws_database_desktop_mobile_and_plan(
    configuration, postgres_store, tmp_path
):
    """Mac lines 29562–29589: full hello alone provides usable map and waypoint.

    Dispatch stays disabled. No readiness message is ever sent by the test peer.
    The model is the harness's explicit local HTTP fixture, not a real provider.
    """
    hello = {
        "type": "hello",
        "robot_id": "test-robot",
        "skills": ["move_to"],
        "map": "hello-only-audit-map（模拟）",
        "waypoint_names": ["研发桌一"],
        "waypoints": 1,
        "preload": "loaded",
        "busy": False,
        "nav_server_ready": True,
    }
    async with platform(configuration, postgres_store, tmp_path) as (app, base, web, prompts):
        assert app.state.execution_enabled is False
        async with connect(
            base + "/api/voice/robot/ws?robot_id=test-robot",
            additional_headers={"x-robot-token": "device"},
            proxy=None,
        ) as ws:
            await ws.recv()
            await ws.send(json.dumps(hello, ensure_ascii=False))
            await ws.recv()

            response = await web.get("/api/robot-agent/robots/test-binding")
            response.raise_for_status()
            detail = response.json()
            assert set(detail["snapshot"]["waypoints"]) == {"研发桌一"}
            assert detail["snapshot"]["readiness"]["nav_server_ready"] is True
            raw = await postgres_store.pool.fetchval(
                "SELECT observations FROM ra_robot_bindings WHERE binding_id='test-binding'"
            )
            observations = json.loads(raw)
            fields = ["map", "waypoint_names", "waypoints", "preload", "busy", "nav_server_ready", "skills"]
            assert len({observations[field]["received_at"] for field in fields}) == 1
            assert len({observations[field]["connection_generation"] for field in fields}) == 1

            async with async_playwright() as pw:
                browser = await pw.chromium.launch(channel="msedge")
                try:
                    page = await browser.new_page(viewport={"width": 1440, "height": 1000})
                    errors = []
                    page.on("pageerror", lambda error: errors.append(str(error)))
                    await page.goto(base.replace("ws:", "http:"))
                    await login(page, "web")
                    await page.get_by_role("link", name="舰队", exact=True).click()
                    await page.locator('[data-binding-id="test-binding"]').click()
                    await expect(page.locator("#robot-fields")).to_contain_text("研发桌一")
                    await expect(page.locator("#robot-fields")).to_contain_text("已就绪")
                    await page.screenshot(path=str(tmp_path / "hello-only-desktop.png"), full_page=True)
                    await page.set_viewport_size({"width": 390, "height": 844})
                    await expect(page.locator("#robot-fields")).to_contain_text("研发桌一")
                    await page.screenshot(path=str(tmp_path / "hello-only-mobile.png"), full_page=True)

                    await page.set_viewport_size({"width": 1440, "height": 1000})
                    await submit(page, "移动到研发桌一")
                    identifier = await page.locator("#task-id").text_content()
                    task_response = await web.get("/api/robot-agent/tasks/" + identifier)
                    task_response.raise_for_status()
                    task = task_response.json()
                    assert task["state"] == "READY"
                    assert task["plan_robot_binding_id"] == "test-binding"
                    assert task["plan_steps"][0]["args"] == {"location": "研发桌一"}
                    await expect(page.locator("#plan")).to_contain_text("研发桌一")
                    assert len(prompts) == 1 and prompts[0]["waypoints"] == ["研发桌一"]
                    assert await postgres_store.pool.fetchval("SELECT count(*) FROM ra_commands") == 0
                    with pytest.raises(TimeoutError):
                        await asyncio.wait_for(ws.recv(), 0.2)
                    assert not errors
                    (tmp_path / "hello-only-evidence.json").write_text(
                        json.dumps(
                            {"sent_frames": [hello], "detail": detail, "observations": observations,
                             "model_inputs": prompts, "task": task, "page_errors": errors,
                             "dispatch_enabled": app.state.execution_enabled},
                            ensure_ascii=False, indent=2,
                        ), encoding="utf-8",
                    )
                finally:
                    await browser.close()
