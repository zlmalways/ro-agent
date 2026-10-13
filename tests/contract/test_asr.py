import asyncio
import gzip
import io
import json
import struct
import wave

import pytest
from websockets.asyncio.server import serve

from robot_agent.adapters.asr import DoubaoASR, DoubaoStream
from robot_agent.domain.models import DependencyUnavailable


async def test_asr_unconfigured_fails_without_fake_transcript():
    with pytest.raises(DependencyUnavailable):
        await DoubaoASR().transcribe(b"test")


def wav_fixture():
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(b"\x00\x00" * 1600)
    return buffer.getvalue()


async def test_doubao_stream_uses_current_seed_wire_and_returns_only_final_text():
    observed = []

    async def protocol_simulator(connection):
        assert connection.request.headers["X-Api-Key"] == "test-secret"
        assert connection.request.headers["X-Api-Resource-Id"] == "test-resource"
        header = await connection.recv()
        assert header[:4] == b"\x11\x10\x11\x00"
        first = json.loads(gzip.decompress(header[8:]))
        observed.append(first)
        while True:
            audio_frame = await connection.recv()
            assert audio_frame[0] == 0x11 and audio_frame[1] >> 4 == 2
            if audio_frame[1] & 0xF == 2:
                break
        payload = gzip.compress(json.dumps({"result": {"text": "test: fetch cup"}}).encode())
        response = b"\x11\x93\x11\x00" + struct.pack(">iI", -1, len(payload)) + payload
        await connection.send(response)

    async with serve(protocol_simulator, "127.0.0.1", 0) as server:
        url = "ws://127.0.0.1:" + str(server.sockets[0].getsockname()[1])
        provider = DoubaoStream(api_key="test-secret", resource_id="test-resource", stream_url=url,
                                timeout_seconds=3, max_response_bytes=4096)
        async with asyncio.timeout(5):
            result = await provider.transcribe(wav_fixture(), filename="test.wav", content_type="audio/wav")
        assert result.text == "test: fetch cup"
        assert observed[0]["audio"] == {"format": "pcm", "codec": "raw", "rate": 16000, "bits": 16, "channel": 1}


async def test_doubao_error_never_becomes_empty_success():
    async def protocol_simulator(connection):
        await connection.recv()
        payload = b"test provider unavailable"
        await connection.send(b"\x11\xf0\x00\x00" + struct.pack(">iI", 500, len(payload)) + payload)
    async with serve(protocol_simulator, "127.0.0.1", 0) as server:
        provider = DoubaoStream(api_key="test-secret", resource_id="test-resource",
                                stream_url="ws://127.0.0.1:" + str(server.sockets[0].getsockname()[1]),
                                timeout_seconds=3, max_response_bytes=4096)
        with pytest.raises(DependencyUnavailable):
            await provider.transcribe(wav_fixture(), filename="test.wav", content_type="audio/wav")
