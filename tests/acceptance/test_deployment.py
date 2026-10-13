import os

import httpx
import pytest

from robot_agent.deployment import build_app


pytestmark = pytest.mark.integration


async def test_explicit_deployment_keeps_execution_disabled_without_approved_host(postgres_store):
    configuration = {
        "database": {"dsn_env": "TEST_DSN", "schema": await postgres_store.pool.fetchval("SELECT current_schema()")},
        "deployment": {"id": "test-deployment", "execution_enabled": False},
        "users": [{"customer_id": "test-customer", "actor_id": "test-user", "roles": ["user"], "token_env": "TEST_USER_TOKEN"}],
        "robots": [{"customer_id": "test-customer", "binding_id": "test-binding", "wire_robot_id": "test-robot",
                    "token_env": "TEST_ROBOT_TOKEN", "allowed_actions": ["move_to", "vla_skill"]}],
        "policy": {"version": "test-policy:v1", "approval_ttl_seconds": 300,
                   "actions": {"test-customer": {"move_to": "allow", "vla_skill": "require_confirmation"}}},
        "limits": {"freshness_seconds": 60, "max_resource_entries": 100, "max_audio_bytes": 1048576,
                   "max_frame_bytes": 8192, "max_text_chars": 2000, "max_plan_steps": 20,
                   "provider_response_bytes": 65536, "scan_interval": 0.1, "command_timeout": 30},
    }
    env = {"TEST_DSN": os.environ["RO_AGENT_TEST_DSN"], "TEST_USER_TOKEN": "test-only-user-token",
           "TEST_ROBOT_TOKEN": "test-only-robot-token"}
    app = build_app(configuration, environment=env)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
            health = await client.get("/health")
            assert health.status_code == 200
            assert health.json()["execution_enabled"] is False
            assert (await client.post("/api/robot-agent/tasks", json={"text": "test"},
                                     headers={"Authorization": "Bearer test-only-user-token", "Idempotency-Key": "key"})).status_code == 503
            assert app.state.service.ownership is None


def test_enabled_execution_requires_all_lock_coordinates():
    with pytest.raises(ValueError, match="lock|host|ownership"):
        build_app({"deployment": {"id": "test", "execution_enabled": True}}, environment={})
