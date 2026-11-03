"""Loopback-only integration deployment for mock-fridge-01. Uses PostgreSQL and the configured providers."""
import os
from contextlib import asynccontextmanager
from urllib.parse import urlsplit
from uuid import uuid4

import asyncpg
import uvicorn

from robot_agent.adapters.voice_output import VoiceOutput
from robot_agent.application.voice_service import VoiceService
from robot_agent.deployment import build_app
from robot_agent.domain.models import TrustedContext
from robot_agent.runtime import Runtime


class SimulatorLease:
    """One dispatcher for this dedicated simulator DB, including on Windows."""

    def __init__(self, dsn):
        self.dsn = dsn
        self.run_id = str(uuid4())
        self.connection = None
        self.disabled = False

    async def acquire(self):
        self.connection = await asyncpg.connect(self.dsn)
        return await self.connection.fetchval("SELECT pg_try_advisory_lock(723418326)")

    async def healthy(self):
        return not self.disabled and self.connection is not None and not self.connection.is_closed()

    def _disable(self):
        self.disabled = True

    async def close(self):
        self.disabled = True
        if self.connection:
            await self.connection.close()


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
    env.setdefault("VOICE_ASR_PROVIDER", "doubao")
    env.setdefault("VOICE_ASR_DOUBAO_RESOURCE_ID", "volc.bigasr.sauc.duration")
    env.setdefault("VOICE_ASR_DOUBAO_STREAM_URL", "wss://openspeech.bytedance.com/api/v3/sauc/bigmodel_async")
    deployment = "local-simulator"
    binding = "mock-fridge-01"
    company = "local-simulator-test"
    app = build_app({
        "database": {"dsn_env": "RO_AGENT_TEST_DSN"},
        "deployment": {"id": deployment, "execution_enabled": False},
        "allow_external_calls": True,
        "users": [{"customer_id": company, "actor_id": "sim-admin", "roles": ["maintainer", "approver"],
                   "token_env": "RO_AGENT_USER_TOKEN"}],
        "robots": [{"customer_id": company, "binding_id": binding, "wire_robot_id": binding,
                    "voice_wire_robot_id": binding + "-voice", "token_env": "RO_AGENT_ROBOT_TOKEN",
                    "allowed_actions": ["move_to", "vla_skill"]}],
        "policy": {"version": "sim-v1", "approval_ttl_seconds": 120,
                   "actions": {company: {"move_to": "allow", "vla_skill": "require_confirmation"}}},
        "limits": {"freshness_seconds": 30, "max_resource_entries": 100, "max_audio_bytes": 2_000_000,
                   "max_frame_bytes": 3_000_000, "max_text_chars": 2000, "max_plan_steps": 12,
                   "provider_response_bytes": 2_000_000, "scan_interval": 0.2, "asr_timeout": 30,
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
            owner = SimulatorLease(dsn)
            service = app.state.service
            service.ownership = owner
            app.state.robot.write_gate = owner
            app.state.voice = voice
            app.state.robot.on_result = voice.on_result
            runtime = Runtime(owner=owner, service=service, deployment_id=deployment, scan_interval=0.2)
            app.state.runtime = runtime
            try:
                await runtime.start()
                app.state.execution_enabled = True
                yield
            finally:
                await runtime.close()
                await voice.close()
    app.router.lifespan_context = local_lifespan
    return app


if __name__ == "__main__":
    uvicorn.run(create_platform(), host="127.0.0.1", port=18763, ws="websockets-sansio",
                ws_max_size=3_000_000)
