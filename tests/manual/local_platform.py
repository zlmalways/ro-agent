"""Loopback-only integration deployment for mock-fridge-01. Uses PostgreSQL and the configured providers."""
import os
import json
from pathlib import Path
import socket
from tempfile import gettempdir
from contextlib import asynccontextmanager
from urllib.parse import urlsplit
import uvicorn

from robot_agent.adapters.voice_output import VoiceOutput
from robot_agent.application.voice_service import VoiceService
from robot_agent.deployment import build_app
from robot_agent.domain.models import TrustedContext


def create_platform():
    env = dict(os.environ)
    dsn = env.get("RO_AGENT_TEST_DSN", "")
    target = urlsplit(dsn)
    if target.hostname != "127.0.0.1" or target.path != "/ro_agent_sim_test" or target.query:
        raise ValueError("use a dedicated local database named ro_agent_sim_test")
    if not all(env.get(name) for name in ("RO_AGENT_ROBOT_TOKEN", "RO_AGENT_USER_TOKEN")):
        raise ValueError("set the simulator robot token and test user token")
    if env["RO_AGENT_ROBOT_TOKEN"] == env["RO_AGENT_USER_TOKEN"]:
        raise ValueError("use separate test credentials for the user and robot")
    config_file = Path(env["RO_AGENT_CONFIG_FILE"])
    asr_source = json.loads(config_file.read_text(encoding="utf-8-sig"))
    deployment = "local-simulator"
    binding = "mock-fridge-01"
    company = "local-simulator-test"
    app = build_app({
        "database": {"dsn_env": "RO_AGENT_TEST_DSN"},
        "deployment": {"id": deployment, "execution_enabled": True,
                       "host_id": socket.gethostname(),
                       "lock_path": env.get("RO_AGENT_TEST_LOCK_PATH") or
                       str(Path(gettempdir()).resolve() / "ro-agent-local-simulator.lock"),
                       "advisory_key": 723418326},
        "allow_external_calls": True,
        "credentials_file": asr_source["credentials_file"],
        "voice_asr": asr_source["voice_asr"],
        "users": [{"customer_id": company, "actor_id": "sim-admin", "roles": ["maintainer", "approver"],
                   "token_env": "RO_AGENT_USER_TOKEN"}],
        "robots": [{"customer_id": company, "binding_id": binding, "wire_robot_id": binding,
                    "voice_wire_robot_id": binding + "-voice", "token_env": "RO_AGENT_ROBOT_TOKEN",
                    "allowed_actions": ["move_to", "vla_skill"]}],
        "policy": {"version": "sim-v1", "approval_ttl_seconds": 120,
                   "actions": {company: {"move_to": "allow", "vla_skill": "require_confirmation"}}},
        "limits": {"freshness_seconds": 30, "max_resource_entries": 100, "max_audio_bytes": 2_000_000,
                   "max_frame_bytes": 3_000_000, "max_text_chars": 2000, "max_plan_steps": 12,
                   "provider_response_bytes": 2_000_000, "scan_interval": 0.2,
                   "command_timeout": 30},
    }, environment=env)
    base_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def local_lifespan(app):
        async with base_lifespan(app):
            admin = TrustedContext(company, "user", "sim-admin", frozenset({"maintainer"}))
            resources = app.state.resources
            if not await resources.read(admin, "environment"):
                await resources.put(admin, binding, "environment", {
                    "前台": "模拟办公区前台", "充电点": "模拟充电站", "冰箱旁": "模拟办公区冰箱取水处",
                }, ("local-simulator-map",))
            if not await resources.read(admin, "capability"):
                await resources.put(admin, binding, "capability", {
                    "sim-pick": "模拟抓取演示", "take-bottled-water": "在冰箱旁拿取一瓶瓶装水（模拟技能，须先到达冰箱旁）",
                }, ("local-simulator-skills",))
            voice = VoiceService(inputs=app.state.inputs, output=VoiceOutput(None), allow_woken_audio=True,
                                 max_audio_bytes=app.state.limits["max_audio_bytes"], duplicate_policy="per_turn")
            # build_app owns the one Runtime, recovery pass and native execution lock.
            # This wrapper only adds the existing whole-WAV voice path and test resources.
            app.state.voice = voice
            app.state.robot.on_result = voice.on_result
            try:
                yield
            finally:
                await voice.close()
    app.router.lifespan_context = local_lifespan
    return app


if __name__ == "__main__":
    uvicorn.run(create_platform(), host="127.0.0.1", port=18763, ws="websockets-sansio",
                ws_max_size=3_000_000)
