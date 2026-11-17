"""Configuration-only development checks; not real robot acceptance."""
import pytest

from tools.dev_runtime import StartupError, apply_overrides


def test_formal_overrides_preserve_new_common_settings_and_do_not_mutate_source():
    base = {"voice_asr": {"provider": "openai", "timeout_seconds": 90},
            "voice_pcm": {"evaluate_wake": False, "enabled": True},
            "users": [{"token_env": "TEST_TOKEN"}]}
    overrides = {"voice_pcm": {"evaluate_wake": True}, "users": [{"token_env": "FORMAL_TOKEN"}]}
    formal = apply_overrides(base, overrides)
    assert formal["voice_asr"] == base["voice_asr"]
    assert formal["voice_pcm"] == {"evaluate_wake": True, "enabled": True}
    assert formal["users"][0]["token_env"] == "FORMAL_TOKEN"
    formal["voice_asr"]["timeout_seconds"] = 1
    assert base["voice_asr"]["timeout_seconds"] == 90
    assert base["users"][0]["token_env"] == "TEST_TOKEN"


def test_renamed_field_stops_instead_of_silently_creating_obsolete_setting():
    with pytest.raises(StartupError, match=r"application.database.dsn_env"):
        apply_overrides({"database": {"new_reference": "TEST"}},
                        {"database": {"dsn_env": "FORMAL"}})


@pytest.mark.parametrize("changed", [[], "true", 1])
def test_changed_field_type_is_not_silently_coerced(changed):
    with pytest.raises(StartupError, match="类型不一致"):
        apply_overrides({"execution_enabled": True}, {"execution_enabled": changed})
