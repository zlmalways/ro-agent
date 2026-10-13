import asyncio
from dataclasses import dataclass
import gzip
import io
import json
import struct
from uuid import uuid4
import wave

from websockets.asyncio.client import connect
from websockets.exceptions import WebSocketException

from robot_agent.domain.models import DependencyUnavailable, PlanError


@dataclass(frozen=True)
class Transcript:
    text: str
    provider_ref: str


class DoubaoASR:
    def __init__(self, provider=None, *, client=None, api_key="", resource_id="", stream_url="",
                 timeout_seconds=15, max_response_bytes=10 * 1024 * 1024):
        self.provider = provider
        self.client = client
        self.api_key = api_key
        self.resource_id = resource_id
        self.stream_url = stream_url
        self.timeout_seconds = timeout_seconds
        self.max_response_bytes = max_response_bytes

    async def transcribe(self, audio):
        if self.provider is None:
            raise DependencyUnavailable("ASR_UNAVAILABLE")
        if not audio or len(audio) > self.max_response_bytes:
            raise PlanError("AUDIO_INVALID")
        try:
            return await self.provider.transcribe(audio, filename="audio.wav", content_type="audio/wav")
        except TimeoutError as exc:
            raise DependencyUnavailable("ASR_TIMEOUT") from exc


class DoubaoStream:
    def __init__(self, *, api_key, resource_id, stream_url, timeout_seconds, max_response_bytes):
        self.api_key = api_key
        self.resource_id = resource_id
        self.stream_url = stream_url
        self.timeout_seconds = timeout_seconds
        self.max_response_bytes = max_response_bytes

    async def transcribe(self, audio, *, filename, content_type):
        if not self.api_key or not self.resource_id or not self.stream_url:
            raise DependencyUnavailable("ASR_UNAVAILABLE")
        try:
            with wave.open(io.BytesIO(audio), "rb") as wav:
                rate, bits, channels = wav.getframerate(), wav.getsampwidth() * 8, wav.getnchannels()
                pcm = wav.readframes(wav.getnframes())
        except (wave.Error, EOFError) as exc:
            raise PlanError("ASR_REQUIRES_PCM_WAV") from exc
        if rate != 16000 or bits != 16 or channels != 1:
            raise PlanError("ASR_REQUIRES_16K_MONO_16BIT_WAV")
        reference = str(uuid4())
        try:
            async with asyncio.timeout(self.timeout_seconds):
                async with connect(self.stream_url, additional_headers={"X-Api-Key": self.api_key,
                                                                         "X-Api-Resource-Id": self.resource_id,
                                                                         "X-Api-Request-Id": reference},
                                   max_size=self.max_response_bytes, open_timeout=self.timeout_seconds) as ws:
                    body = {"user": {"uid": "ro-agent"},
                            "audio": {"format": "pcm", "codec": "raw", "rate": rate, "bits": bits, "channel": channels},
                            "request": {"model_name": "bigmodel", "enable_itn": True, "enable_punc": True, "enable_ddc": True}}
                    await ws.send(seed_frame(1, json.dumps(body).encode(), json_payload=True))
                    reader = asyncio.create_task(self._read_final(ws, reference))
                    try:
                        chunk_size = rate * channels * (bits // 8) // 10
                        for offset in range(0, len(pcm), chunk_size):
                            await ws.send(seed_frame(2, pcm[offset:offset + chunk_size]))
                        await ws.send(seed_frame(2, b"", last=True))
                        return await reader
                    finally:
                        if not reader.done():
                            reader.cancel()
                            try:
                                await reader
                            except asyncio.CancelledError:
                                pass
        except (TimeoutError, OSError, WebSocketException) as exc:
            raise DependencyUnavailable("ASR_UNAVAILABLE") from exc

    async def _read_final(self, ws, reference):
        while True:
            raw = await ws.recv()
            if not isinstance(raw, bytes) or len(raw) < 8:
                raise DependencyUnavailable("ASR_INVALID_RESPONSE")
            header_size = (raw[0] & 15) * 4
            message_type, flags = raw[1] >> 4, raw[1] & 15
            offset = header_size + (4 if flags in (1, 3) or message_type in (11, 15) else 0)
            if len(raw) < offset + 4:
                raise DependencyUnavailable("ASR_INVALID_RESPONSE")
            size = struct.unpack(">I", raw[offset:offset + 4])[0]
            if len(raw) != offset + 4 + size:
                raise DependencyUnavailable("ASR_INVALID_RESPONSE")
            if message_type == 15:
                raise DependencyUnavailable("ASR_PROVIDER_ERROR")
            payload = raw[offset + 4:]
            if raw[2] & 15 == 1:
                with gzip.GzipFile(fileobj=io.BytesIO(payload)) as stream:
                    payload = stream.read(self.max_response_bytes + 1)
            if len(payload) > self.max_response_bytes:
                raise DependencyUnavailable("ASR_RESPONSE_TOO_LARGE")
            value = json.loads(payload) if payload and raw[2] >> 4 == 1 else {}
            if flags in (2, 3) or value.get("is_last_package"):
                text = value.get("result", {}).get("text", "")
                if not isinstance(text, str) or not text.strip():
                    raise PlanError("ASR_EMPTY")
                return Transcript(text.strip(), "doubao:" + reference)


def seed_frame(message_type, payload, *, last=False, json_payload=False):
    compressed = gzip.compress(payload)
    header = bytes((0x11, (message_type << 4) | (2 if last else 0), (0x10 if json_payload else 0) | 1, 0))
    return header + struct.pack(">I", len(compressed)) + compressed
