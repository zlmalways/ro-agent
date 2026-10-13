import httpx
import pytest

from robot_agent.app import create_app


async def test_unconfigured_app_exposes_dependency_status_and_fails_closed():
    app = create_app()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
        status = await client.get("/health")
        assert status.status_code == 503
        assert status.json()["execution_enabled"] is False
        response = await client.post("/api/robot-agent/tasks", json={"text": "test"},
                                     headers={"Authorization": "Bearer invalid", "Idempotency-Key": "test"})
        assert response.status_code == 503


@pytest.mark.integration
async def test_composed_app_uses_supplied_real_store_service(persisted_service):
    from robot_agent.adapters.identity import ConfiguredIdentity
    from robot_agent.application.input_service import InputService
    service, robot = persisted_service
    identity = ConfiguredIdentity(users={"test": {"customer_id": "test-customer", "actor_id": "test-user", "roles": []}}, robots={})
    app = create_app(service=service, identity=identity, inputs=InputService(service, None, max_audio_bytes=1024))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
        response = await client.post("/api/robot-agent/tasks", json={"text": "test"},
                                     headers={"Authorization": "Bearer test", "Idempotency-Key": "key"})
        assert response.status_code == 202
        assert response.json()["state"] == "READY"
        assert robot.sent == []
