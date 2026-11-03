import asyncio
from contextlib import asynccontextmanager
import json
import os
import socket
import sys

import httpx
import pytest

from robot_agent.deployment import build_app


pytestmark = pytest.mark.integration


@pytest.fixture
def configuration():
    return {
        "database": {"dsn_env": "TEST_DSN"},
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


async def test_explicit_deployment_keeps_execution_disabled_without_approved_host(postgres_store, configuration):
    configuration["database"]["schema"] = await postgres_store.pool.fetchval("SELECT current_schema()")
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


@pytest.fixture
def execution_configuration(configuration, tmp_path):
    configuration["deployment"].update(execution_enabled=True, host_id=socket.gethostname(),
                                       lock_path=str(tmp_path / "deployment.lock"), advisory_key=490013)
    return configuration


def test_execution_configuration_accepts_approved_local_host(execution_configuration):
    app = build_app(execution_configuration, environment={"TEST_DSN": "postgresql://localhost/ro_agent_test"})
    assert app.state.execution_enabled is True


@pytest.mark.parametrize(("field", "value", "error"), [
    ("host_id", socket.gethostname() + "-other", "approved.*host"),
    ("lock_path", "relative.lock", "absolute shared lock path"),
])
def test_execution_configuration_rejects_invalid_host_or_path(execution_configuration, field, value, error):
    execution_configuration["deployment"][field] = value
    with pytest.raises(ValueError, match=error):
        build_app(execution_configuration, environment={})


def test_execution_configuration_rejects_missing_database_lock(execution_configuration):
    del execution_configuration["deployment"]["advisory_key"]
    with pytest.raises(ValueError, match="ownership advisory key"):
        build_app(execution_configuration, environment={})


async def test_formal_process_excludes_second_and_allows_takeover(execution_configuration, postgres_store, tmp_path):
    execution_configuration["database"]["schema"] = await postgres_store.pool.fetchval("SELECT current_schema()")
    config_path = tmp_path / "platform.json"
    config_path.write_text(json.dumps(execution_configuration), encoding="utf-8")
    env = dict(os.environ, RO_AGENT_CONFIG_FILE=str(config_path), TEST_DSN=os.environ["RO_AGENT_TEST_DSN"],
               TEST_USER_TOKEN="test-only-user-token", TEST_ROBOT_TOKEN="test-only-robot-token")

    # Use the same factory as uvicorn, with a separate interpreter for each platform.
    code = """
import asyncio, sys
from robot_agent.app import create_app

async def main():
    app = create_app()
    try:
        async with app.router.lifespan_context(app):
            assert await app.state.service.ownership.healthy()
            print("ready", flush=True)
            await asyncio.to_thread(sys.stdin.buffer.read, 1)
    except RuntimeError as exc:
        if str(exc) != "deployment execution ownership unavailable":
            raise
        print("denied", flush=True)

asyncio.run(main())
"""

    @asynccontextmanager
    async def platform():
        process = await asyncio.create_subprocess_exec(sys.executable, "-c", code, env=env,
                                                      stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE)
        try:
            yield (await asyncio.wait_for(process.stdout.readline(), 15)).strip()
        finally:
            process.stdin.close()
            try:
                await asyncio.wait_for(process.wait(), 15)
            except TimeoutError:
                process.kill()
                await process.wait()
                raise
        assert process.returncode == 0

    async with platform() as first:
        assert first == b"ready"
        async with platform() as second:
            assert second == b"denied"
    async with platform() as replacement:
        assert replacement == b"ready"
