"""Whole-WAV Fun Flash transcription; only the complete response reaches the agent.

Protocol: https://help.aliyun.com/zh/model-studio/fun-asr-flash-recorded-speech-recognition-http-api
"""
import asyncio
import base64
import io
import json
from uuid import uuid4
import wave

import httpx

from robot_agent.adapters.asr import Transcript
from robot_agent.asr_config import ALIYUN_FLASH_MODEL
from robot_agent.domain.models import DependencyUnavailable, PlanError


def build_flash_request(audio, model):
    # Keep the previously prohibited Fun Realtime model out of this request path.
    if model != ALIYUN_FLASH_MODEL:
        raise DependencyUnavailable("ASR_MODEL_UNCONFIGURED")
    if not audio:
        raise PlanError("AUDIO_INVALID")
    try:
        with wave.open(io.BytesIO(audio), "rb") as wav:
            rate = wav.getframerate()
            frames = wav.getnframes()
            expected = frames * wav.getnchannels() * wav.getsampwidth()
            if not frames or len(wav.readframes(frames)) != expected:
                raise PlanError("AUDIO_INVALID")
    except (wave.Error, EOFError) as exc:
        raise PlanError("ASR_REQUIRES_PCM_WAV") from exc
    return {
        "model": model,
        "input": {"messages": [{"role": "user", "content": [{
            "type": "input_audio",
            "input_audio": {"data": "data:audio/wav;base64," + base64.b64encode(audio).decode("ascii")},
        }]}]},
        "parameters": {"format": "wav", "sample_rate": str(rate)},
    }


def parse_flash_response(result, model, fallback_reference):
    if not isinstance(result, dict):
        raise DependencyUnavailable("ASR_INVALID_RESPONSE")
    if result.get("code") or result.get("error"):
        raise DependencyUnavailable("ASR_PROVIDER_ERROR")
    output = result.get("output")
    if not isinstance(output, dict) or not isinstance(output.get("text"), str):
        raise DependencyUnavailable("ASR_INVALID_RESPONSE")
    # output.text is accumulated text; sentence.text can be only the last sentence.
    text = output["text"].strip()
    if not text:
        raise PlanError("ASR_EMPTY")
    sentence = output.get("sentence")
    if sentence is not None and (not isinstance(sentence, dict) or sentence.get("sentence_end") is not True):
        raise DependencyUnavailable("ASR_INVALID_RESPONSE")
    reference = result.get("request_id") or output.get("request_id")
    if not isinstance(reference, str) or not reference.strip():
        reference = fallback_reference
    return Transcript(text, f"aliyun:{model}:{reference}")


class AliyunASR:
    def __init__(self, *, api_key, model, base_url, timeout_seconds, max_response_bytes):
        self.api_key = api_key
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.max_response_bytes = max_response_bytes

    async def transcribe(self, audio):
        if not self.api_key or not self.base_url:
            raise DependencyUnavailable("ASR_UNAVAILABLE")
        payload = build_flash_request(audio, self.model)
        reference = "client-" + str(uuid4())
        headers = {"Authorization": "Bearer " + self.api_key, "X-DashScope-SSE": "disable"}
        endpoint = self.base_url + "/api/v1/services/aigc/multimodal-generation/generation"
        try:
            async with asyncio.timeout(self.timeout_seconds), httpx.AsyncClient(
                timeout=self.timeout_seconds, trust_env=False, follow_redirects=False,
            ) as client:
                async with client.stream("POST", endpoint, headers=headers, json=payload) as response:
                    if response.status_code in (401, 403):
                        raise DependencyUnavailable("ASR_AUTH_FAILED")
                    if response.status_code != 200:
                        raise DependencyUnavailable("ASR_PROVIDER_ERROR")
                    if response.headers.get("content-type", "").startswith("text/event-stream"):
                        raise DependencyUnavailable("ASR_INVALID_RESPONSE")
                    body = bytearray()
                    async for chunk in response.aiter_bytes():
                        if len(body) + len(chunk) > self.max_response_bytes:
                            raise DependencyUnavailable("ASR_RESPONSE_TOO_LARGE")
                        body.extend(chunk)
                    reference = (response.headers.get("x-request-id")
                                 or response.headers.get("x-dashscope-request-id") or reference)
        except (TimeoutError, httpx.TimeoutException) as exc:
            raise DependencyUnavailable("ASR_TIMEOUT") from exc
        except httpx.RequestError as exc:
            raise DependencyUnavailable("ASR_UNAVAILABLE") from exc
        try:
            result = json.loads(body)
        except (ValueError, UnicodeError) as exc:
            raise DependencyUnavailable("ASR_INVALID_RESPONSE") from exc
        return parse_flash_response(result, self.model, reference)
