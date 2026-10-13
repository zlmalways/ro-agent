from fastapi import FastAPI
import httpx
import pytest

from robot_agent.adapters.identity import ConfiguredIdentity
from robot_agent.api.resources import router
from robot_agent.application.resources import Resources


pytestmark = pytest.mark.integration


async def test_resource_api_requires_current_identity_and_base_version(postgres_store):
    app = FastAPI()
    app.state.identity = ConfiguredIdentity(users={
        "test-admin": {"customer_id": "test-customer", "actor_id": "admin", "roles": ["maintainer"]},
        "test-user": {"customer_id": "test-customer", "actor_id": "user", "roles": ["user"]},
    }, robots={})
    app.state.resources = Resources(postgres_store, freshness_seconds=60, max_entries=100)
    app.include_router(router)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
        value = {"base_version": 0, "entries": {"table": "test table"}, "source_refs": ["test-survey"]}
        assert (await client.post("/api/robot-agent/resources/environment/versions", json=value)).status_code == 401
        denied = await client.post("/api/robot-agent/resources/environment/versions", json=value,
                                   headers={"Authorization": "Bearer test-user"})
        assert denied.status_code == 403
        first = await client.post("/api/robot-agent/resources/environment/versions", json=value,
                                  headers={"Authorization": "Bearer test-admin"})
        assert first.status_code == 201
        assert first.json()["revision"] == 1
        stale = await client.post("/api/robot-agent/resources/environment/versions", json=value,
                                  headers={"Authorization": "Bearer test-admin"})
        assert stale.status_code == 409
        current = await client.get("/api/robot-agent/resources/environment", headers={"Authorization": "Bearer test-user"})
        assert current.status_code == 200
        assert current.json()["entries"] == {"table": "test table"}
