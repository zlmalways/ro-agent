import asyncio
from contextlib import asynccontextmanager, suppress
from datetime import datetime, timedelta, timezone
import json
import os
from uuid import uuid4

import httpx
import pytest

from robot_agent.adapters.llm import OpenAICompatiblePlanner
from robot_agent.config import ProviderConfig
from robot_agent.deployment import build_app
from tests.integration.test_robot_ws import live_app
from tests.manual.mock_robot_ws import Simulator, command_session


pytestmark = pytest.mark.integration


@asynccontextmanager
async def reporting_robot(base):
    robot = Simulator("test-robot", "一楼", ["门口"], ["拿水", "测试技能"], 0.1)
    client = asyncio.create_task(command_session(base + "/api/voice/robot/ws", "device", robot, interval=300))
    try:
        yield
    finally:
        client.cancel()
        with suppress(asyncio.CancelledError):
            await client


@pytest.mark.parametrize("enabled", [False, True], ids=["default", "reported"])
async def test_reported_waypoints_in_web_and_planning(configuration, postgres_store, enabled):
    if enabled:
        configuration["resources"] = {"use_reported_waypoints": True}
    configuration["database"]["schema"] = await postgres_store.pool.fetchval("SELECT current_schema()")
    configuration["users"][0]["roles"].append("maintainer")
    app = build_app(configuration, environment={"TEST_DSN": os.environ["RO_AGENT_TEST_DSN"],
                                                "TEST_ROBOT_TOKEN": "device", "TEST_USER_TOKEN": "web"})
    prompts = []

    def propose_goal(request):
        # A deliberately untrusted model: even if the requested destination is absent, propose it.
        context = json.loads(json.loads(request.content)["messages"][-1]["content"])
        prompts.append(context)
        goal = context["goal"]
        action, args = ("vla_skill", {"skill_id": goal}) if goal in {"拿水", "测试技能"} else ("move_to", {"location": goal})
        candidate = {"outcome": "plan", "steps": [{"step_id": "s1", "action": action, "args": args, "depends_on": []}]}
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(candidate)}}]})

    async with (app.router.lifespan_context(app), live_app(app) as base, reporting_robot(base),
                httpx.AsyncClient(transport=httpx.MockTransport(propose_goal)) as model,
                httpx.AsyncClient(base_url=base.replace("ws:", "http:"), trust_env=False,
                                  headers={"Authorization": "Bearer web"}) as web):
        app.state.service.planner = OpenAICompatiblePlanner(ProviderConfig("gpt", "http://model.test", "test", "test", 5),
                                                           client=model)

        async def snapshot():
            response = await web.get("/api/robot-agent/resources/snapshot")
            response.raise_for_status()
            return response.json()

        async def submit(target):
            response = await web.post("/api/robot-agent/tasks", headers={"Idempotency-Key": str(uuid4())},
                                      json={"text": target})
            response.raise_for_status()
            return response.json()

        async with asyncio.timeout(5):
            while "waypoint_names" not in (await snapshot())["readiness_evidence"]:
                await asyncio.sleep(0.01)

        assert set((await snapshot())["waypoints"]) == ({"门口"} if enabled else set())
        for kind, entries in [("environment", {"仅登记": "仅登记"}), ("capabilities", {"拿水": "已登记技能"})]:
            res = await web.post(f"/api/robot-agent/resources/{kind}/versions",
                                 json={"entries": entries, "base_version": 0, "source_refs": ["test"]})
            res.raise_for_status()

        shown = await snapshot()
        assert set(shown["waypoints"]) == ({"门口"} if enabled else set())
        assert set(shown["brain_skills"]) == {"拿水"}
        task = await submit("门口")
        assert prompts[-1]["waypoints"] == (["门口"] if enabled else [])
        assert task["state"] == ("READY" if enabled else "FAILED")
        if enabled:
            assert task["plan_steps"][0]["source_refs"] == [shown["waypoints"]["门口"]]
            assert "仅登记" not in prompts[-1]["environment_labels"]
        else:
            assert task["reason_code"] == "PLAN_INVALID"

        assert (await submit("仅登记"))["reason_code"] == "PLAN_INVALID"
        assert (await submit("从未上报"))["reason_code"] == "PLAN_INVALID"

        assert (await submit("拿水"))["state"] == "WAITING_APPROVAL"
        assert (await submit("测试技能"))["reason_code"] == "PLAN_INVALID"

        # Expire just the actual report. Navigation and the skill list are still fresh.
        stale = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
        await postgres_store.pool.execute("""UPDATE ra_robot_bindings SET observations =
            jsonb_set(observations, '{waypoint_names,received_at}', to_jsonb($1::text))
            WHERE binding_id='test-binding'""", stale)
        shown = await snapshot()
        assert not shown["waypoints"] and shown["readiness"]["nav_server_ready"]
        assert set(shown["brain_skills"]) == {"拿水"}
        assert (await submit("门口"))["reason_code"] == "PLAN_INVALID"
        assert prompts[-1]["waypoints"] == []
