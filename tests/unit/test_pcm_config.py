import pytest

from robot_agent.config import PcmSegmentConfig, load_pcm_segment_config


def test_defaults_do_not_depend_on_asr_or_llm_settings():
    assert load_pcm_segment_config({"OPENAI_MODEL": "ignored"}) == PcmSegmentConfig()


def test_configuration_overrides_are_typed_and_validated():
    config = load_pcm_segment_config({
        "VOICE_PCM_SILENCE_MS": "800", "VOICE_PCM_MIN_SPEECH_MS": "320",
        "VOICE_PCM_MAX_SEGMENT_MS": "20000", "VOICE_PCM_PREROLL_MS": "256",
        "VOICE_PCM_THRESHOLD_START": "0.7", "VOICE_PCM_THRESHOLD_CONTINUE": "0.4",
        "VOICE_PCM_MAX_CHUNK_BYTES": "64000",
    })
    assert config == PcmSegmentConfig(800, 320, 20000, 256, 0.7, 0.4, 64000)


@pytest.mark.parametrize("value", ["not-a-number", "-1", "0"])
def test_bad_environment_setting_is_not_silently_replaced_by_default(value):
    with pytest.raises(ValueError):
        load_pcm_segment_config({"VOICE_PCM_SILENCE_MS": value})
