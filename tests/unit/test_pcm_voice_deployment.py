"""PCM-to-task is an explicit local-test configuration, never implied by audio."""
import pytest

from robot_agent.deployment import build_pcm_segments, build_pcm_voice


def test_capture_defaults_do_not_assemble_task_submission():
    assert build_pcm_voice({}, object()) is None
    assert build_pcm_voice({'voice_pcm': {'enabled': True}}, object()) is None


@pytest.mark.parametrize('settings', [
    {'enabled': True, 'submit_tasks': 'true'},
    {'enabled': True, 'submit_tasks': 1},
    {'enabled': False, 'submit_tasks': True},
    {'submit_tasks': True},
])
def test_invalid_or_disabled_submission_fails_during_assembly(settings):
    with pytest.raises(ValueError):
        build_pcm_segments({'voice_pcm': settings}, {})
    with pytest.raises(ValueError):
        build_pcm_voice({'voice_pcm': settings}, object())


def test_explicit_submission_reuses_the_input_service_without_wav_wake_bypass():
    inputs = object()
    service = build_pcm_voice({'voice_pcm': {'enabled': True, 'submit_tasks': True},
                              'limits': {'max_audio_bytes': 65536}}, inputs)
    assert service.inputs is inputs
    assert service.allow_unwoken_pcm is True
    assert service.allow_woken_audio is False
    assert service.max_audio_bytes == 65536
