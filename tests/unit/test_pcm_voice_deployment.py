"""PCM-to-task is an explicit local-test configuration, never implied by audio."""
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from robot_agent.asr_config import load_asr_config

from robot_agent import deployment
from robot_agent.application.voice_service import VoiceService

from robot_agent.deployment import build_pcm_segments, build_pcm_voice


def test_capture_defaults_do_not_assemble_task_submission():
    assert build_pcm_voice({}, object()) is None
    assert build_pcm_voice({'voice_pcm': {'enabled': True}}, object()) is None


@pytest.mark.parametrize('settings', [
    {'enabled': True, 'submit_tasks': 'true'},
    {'enabled': True, 'submit_tasks': 1},
    {'enabled': False, 'submit_tasks': True},
    {'submit_tasks': True},
])
def test_invalid_or_disabled_submission_fails_during_assembly(settings):
    with pytest.raises(ValueError):
        build_pcm_segments({'voice_pcm': settings}, {})
    with pytest.raises(ValueError):
        build_pcm_voice({'voice_pcm': settings}, object())


def test_explicit_submission_reuses_the_input_service_without_wav_wake_bypass():
    inputs = object()
    service = build_pcm_voice({'voice_pcm': {'enabled': True, 'submit_tasks': True},
                              'limits': {'max_audio_bytes': 65536}}, inputs)
    assert service.inputs is inputs
    assert service.allow_unwoken_pcm is True
    assert service.allow_woken_audio is False
    assert service.max_audio_bytes == 65536


@pytest.mark.parametrize("start_fails", [False, True], ids=["normal-shutdown", "startup-failure"])
async def test_pcm_assembly_preserves_main_runtime_resources_and_cleanup(monkeypatch, start_fails):
    """Exercise the combined assembly; only DB/runtime boundaries are simulated."""
    events, provisioned = [], []

    async def close_pool():
        events.append("pool-close")

    pool = SimpleNamespace(close=close_pool)
    monkeypatch.setattr(deployment.asyncpg, "create_pool", AsyncMock(return_value=pool))

    class StoreFixture:
        def __init__(self, received_pool, *, deployment_id):
            assert received_pool is pool and deployment_id == "test-deployment"

        async def migrate(self):
            pass

        @asynccontextmanager
        async def system_unit_of_work(self):
            yield self

        async def provision_binding(self, binding):
            provisioned.append(binding["binding_id"])

    class RuntimeFixture:
        def __init__(self, *, owner, service, deployment_id, scan_interval):
            assert owner is None and deployment_id == "test-deployment"
            self.service = service

        async def start(self):
            events.append("runtime-start")
            if start_fails:
                raise RuntimeError("simulated startup failure")

        async def close(self):
            events.append("runtime-close")

    async def close_voice(self):
        events.append("voice-close")

    monkeypatch.setattr(deployment, "PostgresStore", StoreFixture)
    monkeypatch.setattr(deployment, "Runtime", RuntimeFixture)
    monkeypatch.setattr(VoiceService, "close", close_voice)
    robots = [{"customer_id": "test", "binding_id": name, "wire_robot_id": name,
               "voice_wire_robot_id": name + "-voice", "token_env": name + "-token"}
              for name in ("first", "second", "missing")]
    configuration = {
        "database": {"dsn_env": "TEST_DSN"},
        "deployment": {"id": "test-deployment", "execution_enabled": False},
        "allow_external_calls": True,
        "voice_asr": {"provider": "openai", "mode": "file", "model": "test-transcriber",
                      "timeout_seconds": 3},
        "resources": {"use_reported_waypoints": True},
        "voice_pcm": {"enabled": True, "submit_tasks": True},
        "robots": robots,
        "limits": {"freshness_seconds": 60, "max_resource_entries": 100, "max_audio_bytes": 65536,
                   "max_frame_bytes": 8192, "max_text_chars": 2000, "max_plan_steps": 20,
                   "provider_response_bytes": 65536, "scan_interval": 0.1, "command_timeout": 30,
                   },
    }
    environment = {
        "TEST_DSN": "postgresql://localhost/unused_test", "first-token": "fixture-first",
        "second-token": "fixture-second",
        "DEEPLEAP_LLM_PROVIDER": "relay", "RELAY_MODEL": "test-planner",
        "RELAY_API_KEY": "fixture-relay", "RELAY_BASE_URL": "https://relay.invalid/v1",
        "VOICE_PCM_SILENCE_MS": "640",
    }
    asr_config = load_asr_config(configuration, credentials={"VOICE_ASR_OPENAI_API_KEY": "fixture-asr"})
    app = deployment.build_app(configuration, environment=environment, asr_config=asr_config)

    async def exercise():
        async with app.router.lifespan_context(app):
            assert app.state.runtime.service is app.state.service
            assert app.state.voice.inputs is app.state.inputs
            assert app.state.inputs.service is app.state.service
            assert app.state.resources.connected_robots == app.state.robot.connected_robots
            assert app.state.resources.use_reported_waypoints is True
            assert app.state.resources.missing_credentials == {("test", "missing")}
            assert app.state.inputs.asr.model == "test-transcriber"
            assert app.state.inputs.asr.api_key == "fixture-asr"
            assert app.state.service.planner.config.model == "test-planner"
            assert app.state.service.planner.config.api_key == "fixture-relay"
            assert app.state.pcm_segments.config.silence_ms == 640
            for name in ("first", "second"):
                binding = await app.state.identity.resolve_robot("fixture-" + name, name + "-voice")
                assert binding["binding_id"] == name and binding["channel"] == "voice"

    if start_fails:
        with pytest.raises(RuntimeError, match="simulated startup failure"):
            await exercise()
    else:
        await exercise()
    assert provisioned == ["first", "second", "missing"]
    assert events == ["runtime-start", "runtime-close", "voice-close", "pool-close"]
