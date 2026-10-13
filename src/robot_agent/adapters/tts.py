"""Configured external synthesis; no generated silence or fake success."""

import asyncio
import base64
import io
import json
from uuid import uuid4
import wave

import httpx

from robot_agent.domain.models import DependencyUnavailable


class DoubaoTTS:
    def __init__(self, *, endpoint, api_key, resource_id, speaker, sample_rate, timeout_seconds, max_audio_bytes, client=None):
        self.endpoint = endpoint
        self.api_key = api_key
        self.resource_id = resource_id
        self.speaker = speaker
        self.sample_rate = sample_rate
        self.timeout_seconds = timeout_seconds
        self.max_audio_bytes = max_audio_bytes
        self.client = client

    async def synthesize(self, text):
        if not self.endpoint or not self.api_key or not self.resource_id or not self.speaker:
            raise DependencyUnavailable("TTS_UNAVAILABLE")
        owns = self.client is None
        client = self.client or httpx.AsyncClient(timeout=self.timeout_seconds)
        try:
            async with asyncio.timeout(self.timeout_seconds):
                async with client.stream("POST", self.endpoint,
                                         headers={"X-Api-Key": self.api_key, "X-Api-Resource-Id": self.resource_id,
                                                  "X-Api-Request-Id": str(uuid4()), "Accept": "text/event-stream"},
                                         json={"user": {"uid": "ro-agent"}, "req_params": {"text": text, "speaker": self.speaker,
                                               "audio_params": {"format": "pcm", "sample_rate": self.sample_rate}}}) as response:
                    if response.status_code != 200:
                        raise DependencyUnavailable("TTS_PROVIDER_ERROR")
                    pcm = bytearray()
                    async for line in response.aiter_lines():
                        if not line.startswith("data:"):
                            continue
                        payload = line[5:].strip()
                        if payload == "[DONE]":
                            break
                        event = json.loads(payload)
                        if event.get("code") not in {None, 0, 20000000}:
                            raise DependencyUnavailable("TTS_PROVIDER_ERROR")
                        data = event.get("data")
                        if isinstance(data, str) and data:
                            pcm.extend(base64.b64decode(data, validate=True))
                        if len(pcm) > self.max_audio_bytes:
                            raise DependencyUnavailable("TTS_RESPONSE_TOO_LARGE")
                    if not pcm or len(pcm) % 2:
                        raise DependencyUnavailable("TTS_EMPTY_OR_INVALID_AUDIO")
                    output = io.BytesIO()
                    with wave.open(output, "wb") as wav:
                        wav.setnchannels(1)
                        wav.setsampwidth(2)
                        wav.setframerate(self.sample_rate)
                        wav.writeframes(bytes(pcm))
                    return output.getvalue()
        except (TimeoutError, httpx.HTTPError, ValueError) as exc:
            raise DependencyUnavailable("TTS_UNAVAILABLE") from exc
        finally:
            if owns:
                await client.aclose()
