"""Exercise the actual authenticated WS route; VAD is a labelled test fixture."""
import base64
import asyncio
import io
import struct
import threading
from types import SimpleNamespace
import wave

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest
from starlette.websockets import WebSocketDisconnect

from robot_agent.adapters.identity import ConfiguredIdentity
from robot_agent.api.robot_ws import _pcm_worker, router
from robot_agent.application.pcm_segment_service import PcmSegmentService
from robot_agent.config import PcmSegmentConfig


class LabelledVad:
    def probability(self, chunk):
        return 0.9 if struct.unpack_from("<h", chunk)[0] else 0.0

    def reset(self):
        pass


def window(value, count):
    return struct.pack("<h", value) * 512 * count


def make_app(*, enabled=True, chunk_limit=320000):
    app = FastAPI()
    app.include_router(router)
    app.state.robot = SimpleNamespace(max_frame_bytes=500000)
    app.state.identity = ConfiguredIdentity(users={}, robots={"pcm-token": {
        "customer_id": "test", "binding_id": "test", "wire_robot_id": "command",
        "voice_wire_robot_id": "voice"}})
    app.state.pcm_segments = PcmSegmentService(
        PcmSegmentConfig(preroll_ms=0, max_chunk_bytes=chunk_limit), vad_factory=LabelledVad
    ) if enabled else None
    # Any attempt to route PCM into existing inputs is a test failure.
    class ForbiddenInputs:
        async def audio(self, *args, **kwargs):
            pytest.fail("PCM segmentation must not call ASR or task submission")
    app.state.inputs = ForbiddenInputs()
    return app


def send(ws, pcm, **extra):
    ws.send_json({"type": "audio", "pcm_b64": base64.b64encode(pcm).decode(), **extra})


def open_voice(client, token="pcm-token"):
    return client.websocket_connect("/api/voice/robot/ws?robot_id=voice", headers={"x-robot-token": token})


def test_pcm_segment_returns_wav_without_wake_asr_or_task_service():
    app = make_app()
    original = window(1000, 10) + window(0, 16)
    with TestClient(app) as client, open_voice(client) as ws:
        assert ws.receive_json()["state"] == "ready"
        send(ws, original[:1001])
        send(ws, original[1001:])
        assert ws.receive_json()["state"] == "thinking"
        segment = ws.receive_json()
        assert segment["type"] == "pcm_segment"
        assert segment["duration_ms"] == 832 and segment["end_reason"] == "silence"
        assert segment["received_chunks"] == 2
        assert segment["received_pcm_bytes"] == len(original)
        with wave.open(io.BytesIO(base64.b64decode(segment["wav_b64"])), "rb") as audio:
            assert audio.readframes(audio.getnframes()) == original
        assert ws.receive_json()["state"] == "idle"
        # Same connection can collect another sentence; client hints never trigger ASR.
        send(ws, original, woke=True, member_id="forged")
        assert ws.receive_json()["state"] == "thinking"
        second = ws.receive_json()
        assert second["segment_id"] != segment["segment_id"]
        assert second["received_chunks"] == 3
        assert ws.receive_json()["state"] == "idle"


def test_disconnect_closes_segmenter_and_reconnect_does_not_join_old_audio():
    app = make_app()
    sessions = []
    create = app.state.pcm_segments.new_session
    def tracked():
        session = create()
        sessions.append(session)
        return session
    app.state.pcm_segments.new_session = tracked
    with TestClient(app) as client:
        with open_voice(client) as ws:
            ws.receive_json()
            send(ws, window(1000, 10))
            # Barrier: route consumes audio before this malformed message and closes.
            ws.send_json({"type": "invalid"})
            with pytest.raises(WebSocketDisconnect):
                ws.receive_json()
        with open_voice(client) as ws:
            ws.receive_json()
            send(ws, window(0, 16) + window(2000, 10) + window(0, 16))
            assert ws.receive_json()["state"] == "thinking"
            segment = ws.receive_json()
            with wave.open(io.BytesIO(base64.b64decode(segment["wav_b64"])), "rb") as audio:
                assert audio.readframes(audio.getnframes()) == window(2000, 10) + window(0, 16)
            assert ws.receive_json()["state"] == "idle"
    assert len(sessions) == 2
    for session in sessions:
        with pytest.raises(ValueError, match="closed"):
            app.state.pcm_segments.feed_pcm(session, b"")


@pytest.mark.parametrize("payload", [{"type": "audio", "pcm_b64": "!bad!"},
                                    {"type": "audio", "pcm_b64": 123}, [],
                                    {"type": "audio", "pcm_b64": ""}])
def test_invalid_pcm_or_json_shape_closes_cleanly(payload):
    with TestClient(make_app()) as client, open_voice(client) as ws:
        ws.receive_json()
        ws.send_json(payload)
        with pytest.raises(WebSocketDisconnect) as exc:
            ws.receive_json()
        assert exc.value.code == 1003


def test_oversized_pcm_is_rejected_before_vad():
    with TestClient(make_app(chunk_limit=1024)) as client, open_voice(client) as ws:
        ws.receive_json()
        send(ws, window(1000, 2))
        with pytest.raises(WebSocketDisconnect) as exc:
            ws.receive_json()
        assert exc.value.code == 1009


def test_bad_token_cannot_upload_pcm():
    with TestClient(make_app()) as client, open_voice(client, token="wrong") as ws:
        with pytest.raises(WebSocketDisconnect) as exc:
            ws.receive_json()
        assert exc.value.code == 4401


def test_pcm_disabled_does_not_initialize_model():
    with TestClient(make_app(enabled=False)) as client, open_voice(client) as ws:
        assert ws.receive_json()["state"] == "ready"
        with pytest.raises(WebSocketDisconnect) as exc:
            ws.receive_json()
        assert exc.value.code == 1013


@pytest.mark.asyncio
async def test_repeated_cancellation_waits_for_native_worker_and_cleans_created_session():
    started, release, finished = threading.Event(), threading.Event(), threading.Event()
    cleaned = []
    value = object()
    def create():
        started.set()
        assert release.wait(3)
        finished.set()
        return value
    def close(created):
        assert finished.is_set()
        cleaned.append(created)
    task = asyncio.create_task(_pcm_worker(create, cancel_cleanup=close))
    try:
        assert await asyncio.to_thread(started.wait, 1)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert cleaned == [value]
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)


def test_pcm_reset_failure_does_not_leave_existing_voice_registration():
    app = make_app()
    unregistered = []
    class Voice:
        def register(self, *args):
            return "voice-session"
        def unregister(self, session):
            unregistered.append(session)
    class FailingReset(LabelledVad):
        def reset(self):
            raise RuntimeError("test reset failure")
    app.state.voice = Voice()
    app.state.pcm_segments = PcmSegmentService(vad_factory=FailingReset)
    with TestClient(app) as client, open_voice(client) as ws:
        ws.receive_json()
        send(ws, window(1000, 10))
        ws.send_json({"type": "invalid"})
        with pytest.raises(WebSocketDisconnect):
            ws.receive_json()
    assert unregistered == ["voice-session"]


def test_stats_still_obeys_websocket_frame_size_limit():
    app = make_app()
    app.state.robot.max_frame_bytes = 128
    with TestClient(app) as client, open_voice(client) as ws:
        assert ws.receive_json()["state"] == "ready"
        ws.send_json({"type": "stats", "extra": "x" * 129})
        with pytest.raises(WebSocketDisconnect) as exc:
            ws.receive_json()
        assert exc.value.code == 1009
