"""Deterministic PCM contract tests; the VAD below is a test fixture, not speech recognition."""

import io
import math
import struct
import wave

import pytest

from robot_agent.application.pcm_segment_service import PcmSegmentService
from robot_agent.config import PcmSegmentConfig


SAMPLES_PER_WINDOW = 512
BYTES_PER_WINDOW = SAMPLES_PER_WINDOW * 2


def windows(value, count):
    """A labelled 32 ms PCM window, so dropped/repeated sounds are observable."""
    return struct.pack("<h", value) * SAMPLES_PER_WINDOW * count


class LabelledVad:
    """Test-only scores: >=1000 is speech, 500 is weak speech, otherwise silence."""

    def probability(self, pcm_window):
        assert len(pcm_window) == BYTES_PER_WINDOW
        value = struct.unpack_from("<h", pcm_window)[0]
        return 0.9 if value >= 1000 else 0.4 if value == 500 else 0.0

    def reset(self):
        pass


def make_service(**overrides):
    return PcmSegmentService(PcmSegmentConfig(**overrides), vad_factory=LabelledVad)


def read_wav(segment):
    with wave.open(io.BytesIO(segment.wav_bytes), "rb") as audio:
        assert (audio.getframerate(), audio.getnchannels(), audio.getsampwidth()) == (16000, 1, 2)
        assert audio.getcomptype() == "NONE"
        assert segment.duration_ms == audio.getnframes() / 16
        return audio.readframes(audio.getnframes())


def send_chunks(service, session, audio, sizes):
    segments, offset, index = [], 0, 0
    while offset < len(audio):
        size = sizes[index % len(sizes)]
        segments.extend(service.feed_pcm(session, audio[offset:offset + size]))
        offset += size
        index += 1
    return segments


def test_network_chunk_boundaries_do_not_lose_or_duplicate_audio():
    service = make_service(preroll_ms=0)
    audio = windows(1000, 5) + windows(2000, 5) + windows(0, 16)

    segments = send_chunks(service, service.new_session(), audio, [1, 7, 1025, 3, 5000, 333])

    assert len(segments) == 1
    assert read_wav(segments[0]) == audio
    assert segments[0].segment_id
    assert segments[0].end_reason == "silence"


def test_waits_for_the_complete_tail_silence_before_producing_a_wav():
    service = make_service(preroll_ms=0)
    session = service.new_session()
    assert service.feed_pcm(session, windows(1000, 10)) == []
    assert service.feed_pcm(session, windows(0, 15)) == []

    segments = service.feed_pcm(session, windows(0, 1))

    assert len(segments) == 1
    assert read_wav(segments[0]) == windows(1000, 10) + windows(0, 16)


def test_a_short_pause_within_a_request_does_not_split_it():
    service = make_service(preroll_ms=0)
    session = service.new_session()
    first_words = windows(1000, 5) + windows(0, 8)
    last_words = windows(2000, 5) + windows(0, 16)
    assert service.feed_pcm(session, first_words) == []

    segments = service.feed_pcm(session, last_words)

    assert len(segments) == 1
    assert read_wav(segments[0]) == first_words + last_words


def test_silence_alone_never_creates_a_speech_segment():
    service = make_service()
    session = service.new_session()
    for _ in range(20):
        assert service.feed_pcm(session, windows(0, 100)) == []


def test_discarded_short_noise_at_limit_does_not_qualify_later_noise_as_a_tail():
    service = make_service(preroll_ms=0, max_segment_ms=288, min_speech_ms=288, silence_ms=512)
    session = service.new_session()
    assert service.feed_pcm(session, windows(1000, 2) + windows(0, 7)) == []
    assert service.feed_pcm(session, windows(2000, 1) + windows(0, 16)) == []


def test_a_short_noise_pulse_cannot_use_silence_to_meet_minimum_speech():
    service = make_service(preroll_ms=0)
    session = service.new_session()
    assert service.feed_pcm(session, windows(1000, 1) + windows(0, 16)) == []

    segments = service.feed_pcm(session, windows(2000, 9) + windows(0, 16))

    assert len(segments) == 1
    assert read_wav(segments[0]) == windows(2000, 9) + windows(0, 16)


def test_preroll_preserves_quiet_sounds_immediately_before_speech_only():
    service = make_service(preroll_ms=96)
    session = service.new_session()
    assert service.feed_pcm(session, windows(10, 20) + windows(20, 3)) == []

    segments = service.feed_pcm(session, windows(1000, 9) + windows(0, 16))

    assert len(segments) == 1
    assert read_wav(segments[0]) == windows(20, 3) + windows(1000, 9) + windows(0, 16)


def test_long_speech_is_marked_as_cut_and_the_following_audio_is_preserved():
    service = make_service(preroll_ms=0)
    session = service.new_session()
    # 468 windows = 14.976 s. The following windows belong to the next segment.
    first = send_chunks(service, session, windows(1000, 469), [4096])
    assert len(first) == 1
    assert first[0].end_reason == "max_duration"
    assert first[0].duration_ms == 14976
    assert read_wav(first[0]) == windows(1000, 468)

    second = service.feed_pcm(session, windows(2000, 8) + windows(0, 16))

    assert len(second) == 1
    assert second[0].end_reason == "silence"
    assert read_wav(second[0]) == windows(1000, 1) + windows(2000, 8) + windows(0, 16)
    assert first[0].segment_id != second[0].segment_id


def test_one_network_chunk_can_finish_multiple_utterances():
    service = make_service(preroll_ms=0)
    first = windows(1000, 9) + windows(0, 16)
    second = windows(2000, 9) + windows(0, 16)

    segments = service.feed_pcm(service.new_session(), first + second)

    assert len(segments) == 2
    assert [read_wav(s) for s in segments] == [first, second]
    assert len({s.segment_id for s in segments}) == 2


def test_short_tail_after_maximum_cut_keeps_the_end_of_a_long_request():
    service = make_service()
    audio = windows(1000, 475) + windows(0, 16)

    segments = send_chunks(service, service.new_session(), audio, [4096])

    assert len(segments) == 2
    assert [(s.duration_ms, s.end_reason) for s in segments] == [
        (14976, "max_duration"), (736, "silence")
    ]
    assert b"".join(read_wav(s) for s in segments) == audio


def test_weak_tail_after_maximum_cut_uses_the_continuation_threshold():
    service = make_service()
    audio = windows(1000, 468) + windows(500, 7) + windows(0, 16)

    segments = send_chunks(service, service.new_session(), audio, [3073, 123, 8192])

    assert len(segments) == 2
    assert [s.end_reason for s in segments] == ["max_duration", "silence"]
    assert read_wav(segments[1]) == windows(500, 7) + windows(0, 16)
    assert b"".join(read_wav(s) for s in segments) == audio


def test_exact_maximum_cut_followed_by_silence_does_not_create_a_silent_segment():
    service = make_service()
    session = service.new_session()

    segments = send_chunks(service, session, windows(1000, 468) + windows(0, 32), [4096])

    assert len(segments) == 1
    assert segments[0].end_reason == "max_duration"
    assert read_wav(segments[0]) == windows(1000, 468)
    assert service.feed_pcm(session, windows(0, 16)) == []


def test_a_new_short_noise_after_long_speech_ends_is_still_discarded():
    service = make_service(preroll_ms=0)
    session = service.new_session()
    long_request = windows(1000, 475) + windows(0, 16)
    assert len(send_chunks(service, session, long_request, [4096])) == 2

    assert service.feed_pcm(session, windows(9000, 1) + windows(0, 16)) == []
    next_request = windows(2000, 9) + windows(0, 16)
    segments = service.feed_pcm(session, next_request)

    assert len(segments) == 1
    assert read_wav(segments[0]) == next_request


def test_chunk_size_does_not_change_utterance_boundaries():
    service = make_service(preroll_ms=0)
    audio = windows(1000, 9) + windows(0, 16) + windows(2000, 12) + windows(0, 16)
    together = service.feed_pcm(service.new_session(), audio)
    split = send_chunks(service, service.new_session(), audio, [1, 301, 2049, 13, 8192])
    assert [(read_wav(s), s.end_reason) for s in split] == [
        (read_wav(s), s.end_reason) for s in together
    ]


def test_two_robot_connections_do_not_mix_audio():
    service = make_service(preroll_ms=0)
    robot_a, robot_b = service.new_session(), service.new_session()
    assert service.feed_pcm(robot_a, windows(1000, 9)) == []
    assert service.feed_pcm(robot_b, windows(2000, 9)) == []

    result_b = service.feed_pcm(robot_b, windows(0, 16))
    result_a = service.feed_pcm(robot_a, windows(0, 16))

    assert read_wav(result_a[0]) == windows(1000, 9) + windows(0, 16)
    assert read_wav(result_b[0]) == windows(2000, 9) + windows(0, 16)
    assert result_a[0].segment_id != result_b[0].segment_id


def test_reconnection_does_not_inherit_the_old_partial_word_or_byte():
    service = make_service(preroll_ms=0)
    old_session = service.new_session()
    assert service.feed_pcm(old_session, windows(1000, 9) + b"\x12") == []
    service.close_session(old_session)
    with pytest.raises(ValueError):
        service.feed_pcm(old_session, windows(0, 16))
    new_session = service.new_session()
    assert service.feed_pcm(new_session, windows(0, 16)) == []

    segments = service.feed_pcm(new_session, windows(2000, 9) + windows(0, 16))

    assert len(segments) == 1
    assert read_wav(segments[0]) == windows(2000, 9) + windows(0, 16)


def test_vad_state_is_separate_for_each_connection():
    instances = []

    def factory():
        vad = LabelledVad()
        instances.append(vad)
        return vad

    service = PcmSegmentService(vad_factory=factory)
    session_a, session_b = service.new_session(), service.new_session()
    service.feed_pcm(session_a, windows(0, 1))
    service.feed_pcm(session_b, windows(0, 1))
    assert len(instances) == 2
    assert instances[0] is not instances[1]


def test_weak_continuation_is_kept_but_does_not_start_a_new_utterance():
    service = make_service(preroll_ms=0, min_speech_ms=64, silence_ms=64)
    session = service.new_session()
    assert service.feed_pcm(session, windows(500, 4)) == []
    audio = windows(1000, 3) + windows(500, 4) + windows(0, 2)

    segments = service.feed_pcm(session, audio)

    assert len(segments) == 1
    assert read_wav(segments[0]) == audio


def test_invalid_or_oversized_chunk_is_rejected_without_poisoning_session():
    service = make_service(preroll_ms=0, max_chunk_bytes=4096)
    session = service.new_session()
    with pytest.raises((ValueError, TypeError)):
        service.feed_pcm(session, "not PCM bytes")
    with pytest.raises(ValueError):
        service.feed_pcm(session, b"\0" * 4097)
    assert service.feed_pcm(session, b"") == []
    audio = windows(1000, 9) + windows(0, 16)

    segments = send_chunks(service, session, audio, [2048])

    assert len(segments) == 1
    assert read_wav(segments[0]) == audio


def test_vad_failure_is_reported_instead_of_becoming_silence():
    class BrokenVad(LabelledVad):
        def probability(self, pcm_window):
            raise RuntimeError("fixture detector unavailable")

    service = PcmSegmentService(vad_factory=BrokenVad)
    session = service.new_session()
    with pytest.raises(RuntimeError, match="detector unavailable"):
        service.feed_pcm(session, windows(1000, 1))
    with pytest.raises(ValueError):
        service.feed_pcm(session, windows(0, 16))


def test_a_session_cannot_be_used_by_a_different_service():
    owner, other = make_service(preroll_ms=0), make_service(preroll_ms=0)
    session = owner.new_session()
    with pytest.raises(ValueError):
        other.feed_pcm(session, windows(1000, 9))
    segments = owner.feed_pcm(session, windows(2000, 9) + windows(0, 16))
    assert len(segments) == 1
    assert read_wav(segments[0]) == windows(2000, 9) + windows(0, 16)


@pytest.mark.parametrize("not_a_session", [None, {}, "robot-id"])
def test_connection_id_or_dictionary_cannot_impersonate_a_session(not_a_session):
    service = make_service()
    with pytest.raises((TypeError, ValueError)):
        service.feed_pcm(not_a_session, windows(1000, 9))


def test_subwindow_configuration_rounds_silence_and_preroll_up():
    service = make_service(preroll_ms=33, silence_ms=65, min_speech_ms=65, max_segment_ms=333)
    session = service.new_session()
    assert service.feed_pcm(session, windows(10, 3)) == []
    assert service.feed_pcm(session, windows(1000, 3) + windows(0, 2)) == []

    segments = service.feed_pcm(session, windows(0, 1))

    assert len(segments) == 1
    assert read_wav(segments[0]) == windows(10, 2) + windows(1000, 3) + windows(0, 3)


def test_duration_limit_rounds_down_so_output_never_exceeds_the_limit():
    service = make_service(preroll_ms=0, silence_ms=64, min_speech_ms=32, max_segment_ms=333)
    session = service.new_session()

    first = service.feed_pcm(session, windows(1000, 11))
    second = service.feed_pcm(session, windows(0, 2))

    assert len(first) == len(second) == 1
    assert first[0].duration_ms == 320
    assert first[0].end_reason == "max_duration"
    assert read_wav(first[0]) + read_wav(second[0]) == windows(1000, 11) + windows(0, 2)


def test_subwindow_minimum_does_not_accept_speech_shorter_than_configured():
    service = make_service(preroll_ms=0, silence_ms=64, min_speech_ms=65)
    session = service.new_session()
    assert service.feed_pcm(session, windows(1000, 2) + windows(0, 2)) == []
    assert len(service.feed_pcm(session, windows(2000, 3) + windows(0, 2))) == 1


@pytest.mark.parametrize("overrides", [
    {"silence_ms": 0},
    {"min_speech_ms": 0},
    {"max_segment_ms": 0},
    {"preroll_ms": -1},
    {"max_chunk_bytes": 0},
    {"threshold_start": -0.1},
    {"threshold_start": 1.1},
    {"threshold_continue": -0.1},
    {"threshold_continue": 1.1},
    {"threshold_continue": 0.6, "threshold_start": 0.5},
    {"threshold_start": math.nan},
    {"threshold_continue": math.inf},
    {"min_speech_ms": 1024, "max_segment_ms": 512},
    {"preroll_ms": 1024, "max_segment_ms": 512},
    {"preroll_ms": 64, "min_speech_ms": 64, "max_segment_ms": 127},
    {"silence_ms": math.inf},
    {"min_speech_ms": math.nan},
    {"max_segment_ms": math.inf},
    {"max_chunk_bytes": 1.5},
])
def test_invalid_configuration_fails_before_audio_is_processed(overrides):
    with pytest.raises(ValueError):
        config = PcmSegmentConfig(**overrides)
        PcmSegmentService(config, vad_factory=LabelledVad)
