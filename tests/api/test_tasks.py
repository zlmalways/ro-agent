from fastapi import FastAPI
import httpx
import pytest

from robot_agent.adapters.identity import ConfiguredIdentity
from robot_agent.api.tasks import router


pytestmark = pytest.mark.integration
HEADERS = {"Authorization": "Bearer test-user", "Idempotency-Key": "test-key"}


def api_app(service):
    app = FastAPI()
    app.state.service = service
    from robot_agent.application.input_service import InputService
    app.state.inputs = InputService(service, None, max_audio_bytes=1024)
    app.state.identity = ConfiguredIdentity(users={
        "test-user": {"customer_id": "test-customer", "actor_id": "test-user", "roles": ["user"]},
        "test-other": {"customer_id": "other-customer", "actor_id": "other-user", "roles": ["user"]},
    }, robots={})
    app.include_router(router)
    return app


async def test_api_conflict_and_visibility(persisted_service):
    service, robot = persisted_service
    app = api_app(service)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
        assert (await client.post("/api/robot-agent/tasks", json={"text": "test"})).status_code == 401
        response = await client.post("/api/robot-agent/tasks", json={"text": "test"}, headers=HEADERS)
        assert response.status_code == 202
        view = response.json()
        repeat = await client.post("/api/robot-agent/tasks", json={"text": "test"}, headers=HEADERS)
        assert repeat.json()["task_id"] == view["task_id"]
        conflict = await client.post("/api/robot-agent/tasks", json={"text": "other"}, headers=HEADERS)
        assert conflict.status_code == 409
        hidden = await client.get("/api/robot-agent/tasks/" + view["task_id"], headers={"Authorization": "Bearer test-other"})
        assert hidden.status_code == 404
        bad_version = await client.post(f"/api/robot-agent/tasks/{view['task_id']}/cancel", json={"expected_version": -1}, headers=HEADERS)
        assert bad_version.status_code == 409
        assert robot.sent == []


async def test_api_rejects_forged_trusted_fields(persisted_service):
    service, robot = persisted_service
    async with httpx.AsyncClient(transport=httpx.ASGITransport(api_app(service)), base_url="http://test") as client:
        for field in ("customer_id", "approval", "source_refs"):
            result = await client.post("/api/robot-agent/tasks", json={"text": "test", field: "forged"}, headers=HEADERS)
            assert result.status_code == 422
        assert robot.sent == []
