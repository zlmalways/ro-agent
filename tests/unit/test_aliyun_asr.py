"""Development checks only; these do not prove platform or robot acceptance."""
import base64
from copy import deepcopy
import io
import json
from pathlib import Path
import wave

import pytest

from robot_agent.adapters.aliyun_asr import AliyunASR, build_flash_request, parse_flash_response
from robot_agent.asr_config import load_asr_config
from robot_agent.deployment import build_asr
from robot_agent.domain.models import DependencyUnavailable, PlanError
from tools.dev_runtime import read_external_settings


MODEL = "fun-asr-flash-2026-06-15"


def configuration():
    return {
        "allow_external_calls": True,
        "limits": {"provider_response_bytes": 4096},
        "llm": {"provider": "relay"},
        "voice_asr": {
            "provider": "aliyun", "model": MODEL, "mode": "file", "timeout_seconds": 90,
            "base_url": "https://workspace.cn-beijing.maas.aliyuncs.com/",
        },
    }


def recorded_response():
    # Exact provider response from the real offline Flash run, sample 24, 2026-11-12.
    path = Path(__file__).parents[1] / "fixtures/aliyun_flash_response.json"
    return json.loads(path.read_text(encoding="utf-8"))


def test_launcher_configures_flash_without_environment_or_provider_call():
    config = configuration()
    credentials = {
        "VOICE_ASR_ALIYUN_API_KEY": "TEST-only-aliyun-key",
        "VOICE_ASR_PROVIDER": "openai",
        "RELAY_BASE_URL": "https://planner.invalid/v1",
        "RELAY_API_KEY": "TEST-only-planner-key", "RELAY_MODEL": "test-planner",
    }
    direct = load_asr_config(config, credentials=credentials)
    environment, launched = read_external_settings(config, credentials)
    assert direct == launched
    assert not any(name.startswith("VOICE_ASR_") for name in environment)
    assert credentials["VOICE_ASR_ALIYUN_API_KEY"] not in repr(direct)
    adapter = build_asr(direct)
    assert isinstance(adapter, AliyunASR)
    assert adapter.model == MODEL
    assert adapter.base_url == "https://workspace.cn-beijing.maas.aliyuncs.com"


@pytest.mark.parametrize("changes,field", [
    ({"model": "fun-asr-realtime-2026-02-28"}, "model"),
    ({"model": "qwen-audio-3.1-asr-flash"}, "model"),
    ({"model": ""}, "model"),
    ({"mode": "stream"}, "mode"),
    ({"base_url": ""}, "base_url"),
    ({"base_url": "http://example.com"}, "base_url"),
    ({"base_url": "https://user:secret@example.com"}, "base_url"),
    ({"base_url": "https://example.com:bad"}, "base_url"),
    ({"base_url": "https://example.com/api/v1"}, "base_url"),
    ({"base_url": "https://example.com?key=secret"}, "base_url"),
])
def test_invalid_flash_settings_fail_before_credentials_are_read(changes, field):
    config = configuration()
    config["voice_asr"].update(changes)
    with pytest.raises(ValueError, match=field):
        load_asr_config(config)


def test_flash_requires_its_own_key_and_can_be_disabled():
    config = configuration()
    with pytest.raises(ValueError, match="VOICE_ASR_ALIYUN_API_KEY"):
        load_asr_config(config, credentials={"VOICE_ASR_OPENAI_API_KEY": "TEST-only-openai-key"})
    config["allow_external_calls"] = False
    assert load_asr_config(config) is None


@pytest.mark.parametrize("rate", [16000, 48000])
def test_request_preserves_complete_wav_and_uses_its_sample_rate(rate):
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(b"\x00\x00" * 100)
    audio = buffer.getvalue()
    request = build_flash_request(audio, MODEL)
    assert request["model"] == MODEL
    assert request["parameters"] == {"format": "wav", "sample_rate": str(rate)}
    content = request["input"]["messages"][0]["content"][0]
    assert content["type"] == "input_audio"
    prefix, encoded = content["input_audio"]["data"].split(",", 1)
    assert prefix == "data:audio/wav;base64"
    assert base64.b64decode(encoded) == audio
    with pytest.raises(PlanError, match="AUDIO_INVALID"):
        build_flash_request(audio[:-2], MODEL)


def test_prohibited_model_is_rejected_before_audio_or_network_processing():
    with pytest.raises(DependencyUnavailable, match="ASR_MODEL_UNCONFIGURED"):
        build_flash_request(b"", "fun-asr-realtime-2026-02-28")


def test_actual_cloud_response_returns_accumulated_text_and_request_reference():
    result = parse_flash_response(recorded_response(), MODEL, "client-fallback")
    assert result.text == "小千小千，打开冰箱拿一瓶可乐。"
    assert result.provider_ref == f"aliyun:{MODEL}:03b7cb9e-a63f-92f6-b4eb-e4752a6365f6"


def test_parser_never_substitutes_last_sentence_for_missing_full_text():
    response = recorded_response()
    del response["output"]["text"]
    with pytest.raises(DependencyUnavailable, match="ASR_INVALID_RESPONSE"):
        parse_flash_response(response, MODEL, "client-fallback")


@pytest.mark.parametrize("change,error,code", [
    ({"text": "  "}, PlanError, "ASR_EMPTY"),
    ({"text": None}, DependencyUnavailable, "ASR_INVALID_RESPONSE"),
    ({"sentence": {"sentence_end": False}}, DependencyUnavailable, "ASR_INVALID_RESPONSE"),
])
def test_parser_rejects_empty_or_incomplete_response_data(change, error, code):
    response = deepcopy(recorded_response())
    response["output"].update(change)
    with pytest.raises(error, match=code):
        parse_flash_response(response, MODEL, "client-fallback")
