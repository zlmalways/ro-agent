"""Request contract and failure boundary for direct OpenAI file transcription."""
from email import policy
from email.parser import BytesParser
from unittest.mock import AsyncMock

import httpx
import pytest

from robot_agent.application.input_service import InputService
from robot_agent.deployment import build_asr
from robot_agent.domain.models import DependencyUnavailable, PlanError, TrustedContext
from tests.contract.test_asr import wav_fixture


def configured_asr(model="test-transcriber", *, enabled=True, api_key="official-test-key"):
    return build_asr(
        {"allow_external_calls": enabled, "limits": {"asr_timeout": 3, "provider_response_bytes": 4096}},
        {"VOICE_ASR_PROVIDER": "openai", "VOICE_ASR_OPENAI_MODEL": model,
         "VOICE_ASR_OPENAI_API_KEY": api_key, "RELAY_API_KEY": "unrelated-relay-key"},
    )


@pytest.mark.parametrize("model", ["first-test-model", "second-test-model"])
async def test_configured_model_and_original_wav_are_sent_to_official_endpoint(model):
    wav = wav_fixture()

    def receive(request):
        assert request.method == "POST"
        assert str(request.url) == "https://api.openai.com/v1/audio/transcriptions"
        assert request.headers["authorization"] == "Bearer official-test-key"
        header = f"Content-Type: {request.headers['content-type']}\r\n\r\n".encode()
        form = BytesParser(policy=policy.default).parsebytes(header + request.content)
        parts = {p.get_param("name", header="content-disposition"): p for p in form.iter_parts()}
        assert parts["model"].get_payload(decode=True).decode() == model
        assert parts["file"].get_content_type() == "audio/wav"
        assert parts["file"].get_filename().endswith(".wav")
        assert parts["file"].get_payload(decode=True) == wav
        return httpx.Response(200, json={"text": "请去前台。"}, headers={"x-request-id": "req_test"})

    asr = configured_asr(model)
    asr.transport = httpx.MockTransport(receive)
    result = await asr.transcribe(wav)
    assert result.text == "请去前台。"
    assert result.provider_ref == f"openai:{model}:req_test"


@pytest.mark.parametrize(("args", "error"), [
    ({"model": "  "}, "ASR_MODEL_UNCONFIGURED"),
    ({"api_key": ""}, "ASR_UNAVAILABLE"),
    ({"enabled": False}, "ASR_UNAVAILABLE"),
])
async def test_unconfigured_or_disabled_asr_cannot_send(args, error, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("ASR tried to open a client without authorization/configuration")

    monkeypatch.setattr(httpx, "AsyncClient", forbidden)
    with pytest.raises(DependencyUnavailable, match=error):
        await configured_asr(**args).transcribe(wav_fixture())


@pytest.mark.integration
@pytest.mark.parametrize(("failure", "error_type", "code"), [
    ("unauthorized", DependencyUnavailable, "ASR_AUTH_FAILED"),
    ("timeout", DependencyUnavailable, "ASR_TIMEOUT"),
    ("empty", PlanError, "ASR_EMPTY"),
])
async def test_failed_openai_input_never_reaches_submit_or_robot(
    failure, error_type, code, persisted_service, postgres_store, monkeypatch,
):
    def simulate(request):
        if failure == "timeout":
            raise httpx.ReadTimeout("simulated timeout", request=request)
        if failure == "unauthorized":
            return httpx.Response(401, json={"error": {"message": "simulated invalid key"}})
        return httpx.Response(200, json={"text": "  "}, headers={"x-request-id": "req_empty"})

    service, robot = persisted_service
    submit = AsyncMock(wraps=service.submit)
    monkeypatch.setattr(service, "submit", submit)
    asr = configured_asr()
    asr.transport = httpx.MockTransport(simulate)
    inputs = InputService(service, asr, max_audio_bytes=65536)
    context = TrustedContext("test-customer", "user", "test-user")
    with pytest.raises(error_type, match=code):
        await inputs.audio(context, wav_fixture(), "audio/wav", "test-input")

    submit.assert_not_called()
    assert await postgres_store.pool.fetchval("SELECT state FROM ra_inputs") == "FAILED"
    assert await postgres_store.pool.fetchval("SELECT count(*) FROM ra_tasks") == 0
    await service.dispatch_next("test-deployment")
    assert await postgres_store.pool.fetchval("SELECT count(*) FROM ra_commands") == 0
    assert robot.sent == []
