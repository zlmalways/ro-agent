"""Isolated checks for manual entry wiring; never connect to a server or provider."""
from contextlib import asynccontextmanager
from importlib import import_module
from pathlib import Path
import socket
from types import SimpleNamespace

import pytest

from tests.manual import local_platform


@pytest.fixture
def local_env(monkeypatch, tmp_path):
    values = {"RO_AGENT_TEST_DSN": "postgresql://test:fixture@127.0.0.1/ro_agent_sim_test",
              "RO_AGENT_ROBOT_TOKEN": "fixture-robot", "RO_AGENT_USER_TOKEN": "fixture-user",
              "RO_AGENT_TEST_LOCK_PATH": str(tmp_path / "simulator.lock")}
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    return values


@pytest.mark.parametrize("existing", [False, True], ids=["initialize-resources", "keep-resources"])
async def test_manual_platform_reuses_one_base_lifespan_and_wav_voice(monkeypatch, local_env, existing):
    events, writes, configurations = [], [], []
    owner, runtime, inputs = object(), object(), object()

    class ResourcesFixture:
        async def read(self, context, kind):
            assert context.customer_id == "local-simulator-test"
            return {"existing": True} if existing else None

        async def put(self, context, binding, kind, entries, refs):
            assert binding == "mock-fridge-01" and refs
            writes.append((kind, entries))

    state = SimpleNamespace(resources=ResourcesFixture(), inputs=inputs, runtime=runtime,
                            service=SimpleNamespace(ownership=owner),
                            robot=SimpleNamespace(write_gate=owner), limits={"max_audio_bytes": 2000000})

    @asynccontextmanager
    async def base_lifespan(app):
        events.append("base-enter")
        try:
            yield
        finally:
            events.append("base-exit")

    app = SimpleNamespace(state=state, router=SimpleNamespace(lifespan_context=base_lifespan))

    def build(configuration, *, environment):
        configurations.append(configuration)
        assert environment["RO_AGENT_TEST_DSN"] == local_env["RO_AGENT_TEST_DSN"]
        return app

    def forbidden(*args, **kwargs):
        pytest.fail("manual wrapper must not create a second runtime or database connection")

    real_voice = local_platform.VoiceService

    class RecordingVoice(real_voice):
        async def close(self):
            events.append("voice-close")
            await super().close()

    monkeypatch.setattr(local_platform, "build_app", build)
    monkeypatch.setattr(local_platform, "VoiceService", RecordingVoice)
    # These legacy names may be absent once the wrapper delegates to build_app.
    if hasattr(local_platform, "Runtime"):
        monkeypatch.setattr(local_platform, "Runtime", forbidden)
    if hasattr(local_platform, "asyncpg"):
        monkeypatch.setattr(local_platform.asyncpg, "connect", forbidden)
    result = local_platform.create_platform()
    deployment = configurations[0]["deployment"]
    assert deployment["execution_enabled"] is True
    assert deployment["host_id"] == socket.gethostname()
    assert Path(deployment["lock_path"]).is_absolute()
    assert deployment["lock_path"] == local_env["RO_AGENT_TEST_LOCK_PATH"]
    assert deployment["advisory_key"] == 723418326
    async with result.router.lifespan_context(result):
        assert result.state.runtime is runtime
        assert result.state.service.ownership is owner and result.state.robot.write_gate is owner
        assert result.state.voice.inputs is inputs and result.state.voice.allow_woken_audio is True
        assert result.state.voice.duplicate_policy == "per_turn"
        assert result.state.robot.on_result == result.state.voice.on_result
    assert events == ["base-enter", "voice-close", "base-exit"]
    assert {kind for kind, _ in writes} == (set() if existing else {"environment", "capability"})


def test_manual_configuration_uses_the_actual_platform_factory_without_starting_it(local_env):
    app = local_platform.create_platform()
    assert app.state.execution_enabled is True
    assert app.state.limits["max_audio_bytes"] == 2000000


@pytest.mark.parametrize("dsn", [
    "postgresql://127.0.0.1/ro_agent_formal",
    "postgresql://other-host/ro_agent_sim_test",
    "postgresql://127.0.0.1/ro_agent_sim_test?options=test",
])
def test_manual_platform_rejects_other_databases(monkeypatch, local_env, dsn):
    monkeypatch.setenv("RO_AGENT_TEST_DSN", dsn)
    with pytest.raises(ValueError, match="dedicated local database"):
        local_platform.create_platform()


def test_live_acceptance_is_skipped_without_opt_in(monkeypatch):
    monkeypatch.delenv("RO_AGENT_RUN_LIVE_VOICE", raising=False)
    live = import_module("tests.manual.test_live_robot_voice")
    assert live.live_voice_enabled({}) is False
    assert live.live_voice_enabled({"RO_AGENT_RUN_LIVE_VOICE": "0"}) is False
    assert live.live_voice_enabled({"RO_AGENT_RUN_LIVE_VOICE": "1"}) is True


@pytest.mark.parametrize("url", [
    "ws://127.0.0.1:18764/api/voice/robot/ws",
    "ws://localhost:18764/api/voice/robot/ws",
    "wss://[::1]:18764/api/voice/robot/ws",
])
def test_live_acceptance_accepts_explicit_loopback_ws_url(url):
    live = import_module("tests.manual.test_live_robot_voice")
    assert live.configured_test_ws_url({"RO_AGENT_TEST_WS_URL": url}) == url


@pytest.mark.parametrize("url", [
    "", "ws://192.168.1.20:18763/api/voice/robot/ws",
    "http://127.0.0.1:18764/api/voice/robot/ws", "ws://127.0.0.1:18764/wrong",
    "ws://127.0.0.1:18764/api/voice/robot/ws?robot_id=other",
    "ws://user:pass@127.0.0.1:18764/api/voice/robot/ws",
    "ws://127.0.0.1:invalid/api/voice/robot/ws",
])
def test_live_acceptance_rejects_missing_or_invalid_ws_url(url):
    live = import_module("tests.manual.test_live_robot_voice")
    with pytest.raises(ValueError, match="RO_AGENT_TEST_WS_URL"):
        live.configured_test_ws_url({"RO_AGENT_TEST_WS_URL": url})
