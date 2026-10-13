from types import SimpleNamespace

from fastapi import FastAPI
import httpx
import pytest

from robot_agent.adapters.identity import ConfiguredIdentity
from robot_agent.adapters.llm import OpenAICompatiblePlanner
from robot_agent.application.input_service import InputService
from robot_agent.api.tasks import router
from robot_agent.domain.models import DependencyUnavailable, TaskInput, TrustedContext


pytestmark = pytest.mark.integration
CONTEXT = TrustedContext("test-customer", "user", "test-user")


class ASRFixture:
    def __init__(self):
        self.calls = 0

    async def transcribe(self, audio, *, filename="", content_type=""):
        self.calls += 1
        return SimpleNamespace(text="test: fetch cup", provider_ref="test-asr-1")


async def test_audio_and_text_share_submit(persisted_service):
    service, robot = persisted_service
    inputs = InputService(service, ASRFixture(), max_audio_bytes=1024)
    text = await service.submit(CONTEXT, TaskInput("req", "web", "test: fetch cup", ""), "text-key")
    audio = await inputs.audio(CONTEXT, b"test audio fixture", "audio/wav", "audio-key")
    assert audio.plan_id and text.plan_id
    assert audio.state == text.state == "READY"
    assert robot.sent == []


async def test_same_key_audio_retry_returns_existing_task(persisted_service, postgres_store):
    service, _ = persisted_service
    asr = ASRFixture()
    inputs = InputService(service, asr, max_audio_bytes=1024)
    first = await inputs.audio(CONTEXT, b"test audio fixture", "audio/wav", "audio-key")
    asr.transcribe = lambda audio: (_ for _ in ()).throw(AssertionError("retry must not call ASR"))
    second = await inputs.audio(CONTEXT, b"test audio fixture", "audio/wav", "audio-key")
    assert first.task_id == second.task_id
    assert await postgres_store.pool.fetchval("SELECT count(*) FROM ra_tasks") == 1


async def test_unavailable_provider_never_creates_executable_task(persisted_service):
    service, robot = persisted_service
    service.planner = OpenAICompatiblePlanner(None, client=None, max_response_bytes=4096)
    with pytest.raises(DependencyUnavailable):
        await service.submit(CONTEXT, TaskInput("req", "web", "test", ""), "key")
    await service.dispatch_next("test-deployment")
    assert robot.sent == []


async def test_multipart_audio_uses_same_task_service(persisted_service):
    service, robot = persisted_service
    app = FastAPI()
    app.state.service = service
    app.state.inputs = InputService(service, ASRFixture(), max_audio_bytes=1024)
    app.state.identity = ConfiguredIdentity(users={"test-token": {"customer_id": "test-customer", "actor_id": "test-user", "roles": []}}, robots={})
    app.include_router(router)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
        result = await client.post("/api/robot-agent/tasks/audio", files={"audio": ("test.wav", b"fixture", "audio/wav")},
                                   headers={"Authorization": "Bearer test-token", "Idempotency-Key": "audio-key"})
        assert result.status_code == 202
        assert result.json()["state"] == "READY"
        assert robot.sent == []


async def test_failed_asr_input_is_persisted_and_retry_does_not_hang(persisted_service, postgres_store):
    service, robot = persisted_service
    class FailingASR:
        async def transcribe(self, audio):
            raise DependencyUnavailable("ASR_UNAVAILABLE")
    inputs = InputService(service, FailingASR(), max_audio_bytes=1024)
    with pytest.raises(DependencyUnavailable):
        await inputs.audio(CONTEXT, b"fixture", "audio/wav", "key")
    assert await postgres_store.pool.fetchval("SELECT state FROM ra_inputs") == "FAILED"
    with pytest.raises(DependencyUnavailable, match="INPUT_FAILED"):
        await inputs.audio(CONTEXT, b"fixture", "audio/wav", "key")
    assert await postgres_store.pool.fetchval("SELECT count(*) FROM ra_tasks") == 0
    assert robot.sent == []

