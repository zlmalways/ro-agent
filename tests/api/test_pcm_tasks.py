"""The opt-in PCM route shares existing input handling; test doubles never call models."""
import asyncio
import base64
import io
import wave
from types import SimpleNamespace

from fastapi.testclient import TestClient
import pytest
from starlette.websockets import WebSocketDisconnect

from robot_agent.application.pcm_segment_service import AudioSegment, PcmSegmentService
from robot_agent.application.voice_service import VoiceService
from robot_agent.config import PcmSegmentConfig
from robot_agent.domain.models import DependencyUnavailable, PlanError
from tests.api.test_robot_pcm import LabelledVad, make_app, open_voice, send, window


class RecordingInputs:
    def __init__(self):
        self.calls = []
        self.error = None

    async def audio(self, context, audio, content_type, request_key):
        self.calls.append((context, audio, content_type, request_key))
        if self.error:
            raise self.error
        return SimpleNamespace(task_id="task-from-existing-inputs", state="PLANNED")


class ForbiddenOutput:
    async def render(self, **kwargs):
        pytest.fail("PCM task submission must not depend on TTS")


def task_app(*, enabled=True):
    app = make_app()
    inputs = RecordingInputs()
    app.state.voice = VoiceService(inputs=inputs, output=ForbiddenOutput(), allow_woken_audio=False,
                                   max_audio_bytes=100000, allow_unwoken_pcm=enabled)
    return app, inputs


def test_opt_in_pcm_uses_authenticated_identity_segment_key_and_existing_inputs():
    app, inputs = task_app()
    original = window(1000, 10) + window(0, 16)
    with TestClient(app) as client, open_voice(client) as ws:
        assert ws.receive_json()["state"] == "ready"
        send(ws, original, woke=False, member_id="forged-user", robot_id="forged-robot")
        assert ws.receive_json()["state"] == "thinking"
        capture = ws.receive_json()
        result = ws.receive_json()
        assert capture["type"] == "pcm_segment"
        assert result == {"type": "pcm_task", "segment_id": capture["segment_id"],
                          "request_key": capture["segment_id"], "task_id": "task-from-existing-inputs",
                          "state": "PLANNED"}
        assert len(inputs.calls) == 1
        context, audio, content_type, key = inputs.calls[0]
        assert (context.customer_id, context.actor_kind, context.actor_id) == ("test", "robot", "test")
        assert audio == base64.b64decode(capture["wav_b64"])
        assert content_type == "audio/wav" and key == capture["segment_id"]
        assert ws.receive_json()["state"] == "idle"


def test_disabled_pcm_task_mode_remains_capture_even_with_client_wake_hint():
    app, inputs = task_app(enabled=False)
    with TestClient(app) as client, open_voice(client) as ws:
        ws.receive_json()
        send(ws, window(1000, 10) + window(0, 16), woke=True)
        assert ws.receive_json()["state"] == "thinking"
        assert ws.receive_json()["type"] == "pcm_segment"
        assert ws.receive_json()["state"] == "idle"
    assert inputs.calls == []


def test_pcm_input_failure_is_reported_once_and_connection_can_accept_next_sentence():
    app, inputs = task_app()
    inputs.error = DependencyUnavailable("ASR_UNAVAILABLE")
    audio = window(1000, 10) + window(0, 16)
    with TestClient(app) as client, open_voice(client) as ws:
        ws.receive_json()
        send(ws, audio)
        assert ws.receive_json()["state"] == "thinking"
        ws.receive_json()
        error = ws.receive_json()
        assert error["state"] == "error" and error["message"] == "ASR_UNAVAILABLE"
        assert ws.receive_json()["state"] == "idle"
        assert len(inputs.calls) == 1
        inputs.error = None
        send(ws, audio)
        assert ws.receive_json()["state"] == "thinking"
        ws.receive_json()
        assert ws.receive_json()["type"] == "pcm_task"
        assert ws.receive_json()["state"] == "idle"
    assert len(inputs.calls) == 2
    assert inputs.calls[0][3] != inputs.calls[1][3]


def test_overlong_sentence_closes_connection_without_submitting_it_or_its_tail():
    app, inputs = task_app()
    app.state.pcm_segments = PcmSegmentService(
        PcmSegmentConfig(preroll_ms=0, min_speech_ms=64, silence_ms=64, max_segment_ms=320),
        vad_factory=LabelledVad)
    with TestClient(app) as client, open_voice(client) as ws:
        ws.receive_json()
        # Forced cut, spoken tail, then a separate short sentence in the SAME network frame.
        send(ws, window(1000, 12) + window(0, 2) + window(2000, 3) + window(0, 2))
        assert ws.receive_json()["state"] == "thinking"
        assert ws.receive_json()["end_reason"] == "max_duration"
        error = ws.receive_json()
        assert error["state"] == "error" and error["message"] == "PCM_TOO_LONG"
        with pytest.raises(WebSocketDisconnect) as exc:
            ws.receive_json()
        assert exc.value.code == 1009
    assert inputs.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled,reason,error", [(False, "silence", "PCM_TASK_SUBMISSION_DISABLED"),
                                                 (True, "max_duration", "PCM_TOO_LONG")])
async def test_service_enforces_opt_in_and_complete_sentence(enabled, reason, error):
    app, inputs = task_app(enabled=enabled)
    voice = app.state.voice
    session = voice.register({"customer_id": "test", "binding_id": "test", "wire_robot_id": "voice"}, None)
    with pytest.raises(PlanError, match=error):
        await voice.deliver_pcm(session, AudioSegment("one", b"wav", 320, reason))
    assert inputs.calls == []


@pytest.mark.asyncio
async def test_pcm_submission_does_not_return_before_existing_input_finishes():
    app, inputs = task_app()
    voice = app.state.voice
    session = voice.register({"customer_id": "test", "binding_id": "test", "wire_robot_id": "voice"}, None)
    release, started = asyncio.Event(), asyncio.Event()
    original = inputs.audio
    async def delayed(*args):
        started.set()
        await release.wait()
        return await original(*args)
    inputs.audio = delayed
    pending = asyncio.create_task(voice.deliver_pcm(session, AudioSegment("one", b"wav", 320, "silence")))
    try:
        await asyncio.wait_for(started.wait(), 1)
        assert not pending.done()
        release.set()
        assert (await pending)["task_id"] == "task-from-existing-inputs"
    finally:
        release.set()
        await asyncio.gather(pending, return_exceptions=True)


@pytest.mark.parametrize("enabled", [False, True], ids=["capture-only", "task-submission"])
def test_stats_between_pcm_chunks_and_sentences_are_ignored(enabled):
    app, inputs = task_app(enabled=enabled)
    original = window(1000, 10) + window(0, 16)
    # Match the real microphone sender's status frame; fields are not consumed.
    stats = {"type": "stats", "mic_frames": 0, "silent_secs": None,
             "audio_activated": None, "drop_oversize": 0, "drop_decode": 0,
             "drop_queue_full": 0, "resample_fallbacks": 0}
    with TestClient(app) as client, open_voice(client) as ws:
        assert ws.receive_json()["state"] == "ready"
        for sentence in range(2):
            ws.send_json(stats)
            send(ws, original[:1001])
            ws.send_json({**stats, "mic_frames": sentence + 1, "audio_activated": True})
            ws.send_json({"type": "stats"})
            send(ws, original[1001:])
            assert ws.receive_json()["state"] == "thinking"
            capture = ws.receive_json()
            assert capture["type"] == "pcm_segment"
            assert capture["received_chunks"] == 2 * (sentence + 1)
            assert capture["received_pcm_bytes"] == len(original) * (sentence + 1)
            assert capture["duration_ms"] == 832 and capture["end_reason"] == "silence"
            # Decode the returned WAV to prove status messages did not alter PCM.
            with wave.open(io.BytesIO(base64.b64decode(capture["wav_b64"])), "rb") as wav:
                assert wav.readframes(wav.getnframes()) == original
            if enabled:
                result = ws.receive_json()
                assert result["type"] == "pcm_task"
                assert result["segment_id"] == capture["segment_id"]
            assert ws.receive_json()["state"] == "idle"
            ws.send_json(stats)
        # FIFO barrier: trailing stats are consumed, while unknown types still fail.
        ws.send_json({"type": "unknown-status"})
        with pytest.raises(WebSocketDisconnect) as exc:
            ws.receive_json()
        assert exc.value.code == 1003
    assert len(inputs.calls) == (2 if enabled else 0)
