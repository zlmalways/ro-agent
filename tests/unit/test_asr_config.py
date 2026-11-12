import json
from tempfile import TemporaryDirectory
from pathlib import Path

import pytest

from robot_agent.asr_config import load_asr_config
from robot_agent.deployment import build_app, build_asr
from tools.dev_runtime import StartupError, read_external_settings


LIMITS = {"provider_response_bytes": 4096}


def test_json_selects_openai_and_keeps_key_out_of_repr():
    config = {
        "allow_external_calls": True,
        "limits": LIMITS,
        "voice_asr": {"provider": "openai", "mode": "file", "model": "gpt-transcribe",
                      "timeout_seconds": 30},
    }
    resolved = load_asr_config(config, credentials={"VOICE_ASR_OPENAI_API_KEY": "secret"})
    assert resolved.provider == "openai"
    assert resolved.model == "gpt-transcribe"
    assert "secret" not in repr(resolved)


def test_json_selects_doubao_without_provider_environment_variable():
    config = {
        "allow_external_calls": True,
        "limits": LIMITS,
        "voice_asr": {
            "provider": "doubao", "mode": "stream", "model": "bigmodel",
            "resource_id": "volc.bigasr.sauc.duration",
            "stream_url": "wss://openspeech.bytedance.com/api/v3/sauc/bigmodel_async",
            "timeout_seconds": 30,
        },
    }
    resolved = load_asr_config(config, credentials={"VOICE_ASR_DOUBAO_API_KEY": "secret"})
    assert (resolved.provider, resolved.resource_id) == ("doubao", "volc.bigasr.sauc.duration")


def test_selected_provider_requires_its_matching_secret():
    config = {
        "allow_external_calls": True,
        "limits": LIMITS,
        "voice_asr": {"provider": "doubao", "mode": "stream", "model": "bigmodel",
                      "resource_id": "volc.bigasr.sauc.duration",
                      "stream_url": "wss://openspeech.bytedance.com/api/v3/sauc/bigmodel_async",
                      "timeout_seconds": 30},
    }
    with pytest.raises(ValueError, match="VOICE_ASR_DOUBAO_API_KEY"):
        load_asr_config(config, credentials={})


def application(provider="openai"):
    settings = {"provider": provider, "timeout_seconds": 30}
    if provider == "openai":
        settings.update(model="json-selected-model", mode="file")
    else:
        settings.update(mode="stream", model="bigmodel", resource_id="json-selected-resource",
                        stream_url="wss://openspeech.bytedance.com/api/v3/sauc/bigmodel_async")
    return {"allow_external_calls": True, "limits": dict(LIMITS),
            "llm": {"provider": "relay"}, "voice_asr": settings}


@pytest.mark.parametrize("provider", ["openai", "doubao"])
def test_launcher_and_direct_loader_resolve_the_same_json(provider):
    config = application(provider)
    credentials = {
        "RELAY_BASE_URL": "https://planner.invalid/v1", "RELAY_API_KEY": "test-planner-key",
        "RELAY_MODEL": "test-planner", "VOICE_ASR_OPENAI_API_KEY": "test-openai-key",
        "VOICE_ASR_DOUBAO_API_KEY": "test-doubao-key",
        "VOICE_ASR_PROVIDER": "wrong-obsolete-provider", "VOICE_ASR_OPENAI_MODEL": "obsolete-model",
    }
    with TemporaryDirectory(prefix="test-asr-json-") as directory:
        key_file = Path(directory) / "test-credentials.json"
        key_file.write_text(json.dumps(credentials), encoding="utf-8")
        config["credentials_file"] = str(key_file)
        direct = load_asr_config(config)
        llm_environment, launched = read_external_settings(config)
        assert direct == launched
        assert not any(key.startswith("VOICE_ASR_") for key in llm_environment)
        assert llm_environment["DEEPLEAP_LLM_PROVIDER"] == "relay"
        assert direct.api_key == f"test-{provider}-key"
        adapter = build_asr(direct)
        if provider == "openai":
            assert adapter.model == "json-selected-model"
        else:
            assert adapter.provider.resource_id == "json-selected-resource"


def test_environment_cannot_supply_a_missing_json_secret():
    config = application()
    with TemporaryDirectory(prefix="test-asr-json-") as directory:
        key_file = Path(directory) / "test-credentials.json"
        key_file.write_text("{}", encoding="utf-8")
        config["credentials_file"] = str(key_file)
        with pytest.raises(ValueError, match="VOICE_ASR_OPENAI_API_KEY"):
            build_app(config, environment={"VOICE_ASR_OPENAI_API_KEY": "must-not-be-used"})


@pytest.mark.parametrize("provider", ["disabled", None])
def test_text_only_or_disabled_asr_does_not_read_a_key_file(provider):
    config = {"allow_external_calls": True}
    if provider:
        config["voice_asr"] = {"provider": provider}
    assert load_asr_config(config) is None


def test_external_calls_off_never_resolves_provider_credentials():
    config = application()
    config["allow_external_calls"] = False
    assert load_asr_config(config) is None
    assert read_external_settings(config) == ({}, None)


@pytest.mark.parametrize("timeout", [None, 0, -1, True, "30", float("nan"), float("inf")])
def test_invalid_timeout_fails_before_reading_secrets(timeout):
    config = application()
    config["voice_asr"]["timeout_seconds"] = timeout
    with pytest.raises(ValueError, match="timeout_seconds"):
        load_asr_config(config)


@pytest.mark.parametrize("settings,field", [
    ({"provider": "unknown"}, "provider"),
    ({"provider": ["openai"]}, "provider"),
    ([], "voice_asr"),
    ({"provider": "openai", "timeout_seconds": 30}, "model"),
    ({"provider": "openai", "model": "  ", "timeout_seconds": 30}, "model"),
    ({"provider": "doubao", "timeout_seconds": 30}, "resource_id"),
])
def test_incomplete_or_unknown_provider_fails_at_configuration(settings, field):
    config = application()
    config["voice_asr"] = settings
    with pytest.raises(ValueError, match=field):
        load_asr_config(config)


@pytest.mark.parametrize("url", ["http://example.com", "wss://user:secret@example.com/ws",
                                "wss://example.com:bad/ws", "wss://example.com/ws?key=secret"])
def test_invalid_doubao_url_is_rejected_without_echoing_it(url):
    config = application("doubao")
    config["voice_asr"]["stream_url"] = url
    with pytest.raises(ValueError, match="stream_url") as caught:
        load_asr_config(config)
    assert url not in str(caught.value)


@pytest.mark.parametrize("content", ["[]", '{"key": broken-json-secret}'])
def test_launcher_reports_unreadable_json_without_its_contents(content):
    config = application()
    with TemporaryDirectory(prefix="test-asr-json-") as directory:
        key_file = Path(directory) / "test-credentials.json"
        key_file.write_text(content, encoding="utf-8")
        config["credentials_file"] = str(key_file)
        with pytest.raises(StartupError, match="credentials_file") as caught:
            read_external_settings(config)
        assert content not in str(caught.value)
