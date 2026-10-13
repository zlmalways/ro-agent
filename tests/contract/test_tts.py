import base64
import io
import json
import wave

import httpx
import pytest

from robot_agent.adapters.tts import DoubaoTTS
from robot_agent.domain.models import DependencyUnavailable


async def test_doubao_synthesis_produces_valid_wav_from_provider_pcm():
    observed = []
    async def fixture(request):
        observed.append(json.loads(request.content))
        pcm = base64.b64encode(b"\x00\x00" * 2400).decode()
        return httpx.Response(200, text='data: ' + json.dumps({"code": 0, "data": pcm}) + '\n\ndata: [DONE]\n\n')
    async with httpx.AsyncClient(transport=httpx.MockTransport(fixture)) as client:
        provider = DoubaoTTS(endpoint="https://test-tts/sse", api_key="test-secret", resource_id="test-resource", speaker="test-voice",
                             sample_rate=24000, timeout_seconds=2, max_audio_bytes=10000, client=client)
        audio = await provider.synthesize("test fact")
        with wave.open(io.BytesIO(audio), "rb") as wav:
            assert wav.getframerate() == 24000
            assert wav.getnchannels() == 1
            assert wav.getnframes() == 2400
        assert observed[0]["req_params"]["text"] == "test fact"
        assert observed[0]["req_params"]["speaker"] == "test-voice"


async def test_tts_rejects_missing_credential_and_provider_error():
    provider = DoubaoTTS(endpoint="", api_key="", resource_id="", speaker="", sample_rate=24000,
                         timeout_seconds=1, max_audio_bytes=10000)
    with pytest.raises(DependencyUnavailable):
        await provider.synthesize("test")
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, text='data: {"code":500}\n\n'))) as client:
        provider = DoubaoTTS(endpoint="https://test/sse", api_key="test", resource_id="test", speaker="test", sample_rate=24000,
                             timeout_seconds=1, max_audio_bytes=10000, client=client)
        with pytest.raises(DependencyUnavailable):
            await provider.synthesize("test")
