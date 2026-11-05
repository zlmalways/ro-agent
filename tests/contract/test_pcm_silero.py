"""Real optional CPU VAD checks, using the existing spoken '请去前台' fixture.

No ASR/LLM/network/robot/database is used. Install the voice-pcm extra to run.
"""

import io
from pathlib import Path
import struct
import wave

import pytest

from robot_agent.application.pcm_segment_service import PcmSegmentService
from robot_agent.config import PcmSegmentConfig


pytestmark = pytest.mark.voice_pcm


@pytest.fixture
def real_service():
    pytest.importorskip("silero_vad", reason="requires optional ro-agent[voice-pcm] dependencies")
    return PcmSegmentService(PcmSegmentConfig())


def test_real_silero_keeps_all_nonzero_samples_of_front_desk_request(real_service):
    fixture = Path(__file__).resolve().parents[1] / "manual" / "fixtures" / "front-desk.wav"
    with wave.open(str(fixture), "rb") as audio:
        assert (audio.getframerate(), audio.getnchannels(), audio.getsampwidth()) == (16000, 1, 2)
        pcm = audio.readframes(audio.getnframes())
    # One second silence on either side, odd-sized network chunks crossing samples.
    stream = b"\0" * 32000 + pcm + b"\0" * 32000
    session = real_service.new_session()
    try:
        segments = []
        for start in range(0, len(stream), 641):
            segments.extend(real_service.feed_pcm(session, stream[start:start + 641]))
    finally:
        real_service.close_session(session)
    assert len(segments) == 1
    segment = segments[0]
    assert segment.end_reason == "silence"
    with wave.open(io.BytesIO(segment.wav_bytes), "rb") as audio:
        assert (audio.getframerate(), audio.getnchannels(), audio.getsampwidth()) == (16000, 1, 2)
        output = audio.readframes(audio.getnframes())
        assert segment.duration_ms == audio.getnframes() / 16
    # Every output sample must be unchanged and contiguous in the input stream.
    offset = stream.find(output)
    assert offset >= 0 and offset % 2 == 0
    samples = struct.unpack(f"<{len(pcm) // 2}h", pcm)
    nonzero = [i for i, sample in enumerate(samples) if sample != 0]
    assert nonzero
    assert offset <= 32000 + nonzero[0] * 2
    assert offset + len(output) >= 32000 + (nonzero[-1] + 1) * 2


def test_real_silero_silence_does_not_produce_a_wav(real_service):
    session = real_service.new_session()
    try:
        for _ in range(4):
            assert real_service.feed_pcm(session, b"\0" * 32000) == []
    finally:
        real_service.close_session(session)
