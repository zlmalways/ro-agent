"""Real PostgreSQL/InputService/TaskService with explicitly simulated VAD and ASR."""
import asyncio
import base64
import io
import json
import wave

import pytest
from websockets.asyncio.client import connect

from robot_agent.application.input_service import InputService
from robot_agent.application.pcm_segment_service import PcmSegmentService
from robot_agent.application.voice_service import VoiceService
from robot_agent.config import PcmSegmentConfig
from robot_agent.domain.models import TrustedContext
from tests.api.test_pcm_tasks import ForbiddenOutput
from tests.api.test_robot_pcm import LabelledVad, window
from tests.api.test_task_audio import ASRFixture
from tests.integration.test_robot_voice import voice_app
from tests.integration.test_robot_ws import HELLO, live_app


pytestmark = pytest.mark.integration


async def test_pcm_segment_reuses_persisted_asr_task_and_idempotency(persisted_service, postgres_store):
    tasks, robot = persisted_service
    asr = ASRFixture()
    inputs = InputService(tasks, asr, max_audio_bytes=100000)
    voice = VoiceService(inputs=inputs, output=ForbiddenOutput(), allow_woken_audio=False,
                         max_audio_bytes=100000, allow_unwoken_pcm=True)
    session = voice.register({"customer_id": "test-customer", "binding_id": "test-binding",
                              "wire_robot_id": "test-voice"}, None)
    cutter = PcmSegmentService(PcmSegmentConfig(preroll_ms=0), vad_factory=LabelledVad)
    pcm_session = cutter.new_session()
    try:
        segment, = cutter.feed_pcm(pcm_session, window(1000, 10) + window(0, 16))
        first = await voice.deliver_pcm(session, segment)
        second = await voice.deliver_pcm(session, segment)
    finally:
        cutter.close_session(pcm_session)
        voice.unregister(session)
    assert first == second and first["state"] == "RECEIVED"
    assert await postgres_store.pool.fetchval("SELECT count(*) FROM ra_plans") == 0
    # Keep the mainline admission boundary: a task ID is not proof of a completed plan.
    await tasks.schedule_pending("test-deployment", task_id=first["task_id"])
    planned = await tasks.get(TrustedContext("test-customer", "robot", "test-binding"), first["task_id"])
    assert planned.state == "READY" and planned.plan_id
    assert first["request_key"] == first["segment_id"] == segment.segment_id
    assert asr.calls == 1 and robot.sent == []
    assert await postgres_store.pool.fetchval("SELECT count(*) FROM ra_tasks") == 1
    row = await postgres_store.pool.fetchrow(
        "SELECT key,normalized_text,provider_ref,state,result_task_id FROM ra_inputs")
    assert tuple(row) == (segment.segment_id, "test: fetch cup", "test-asr-1", "COMPLETE", first["task_id"])
    task = await postgres_store.pool.fetchrow("SELECT actor_kind,actor_id FROM ra_tasks")
    assert tuple(task) == ("robot", "test-binding")


async def test_pcm_with_stats_enters_mainline_async_admission_and_queue(persisted_service, postgres_store):
    tasks, _ = persisted_service
    app = voice_app(tasks)
    app.state.inputs.max_audio_bytes = 100000
    app.state.robot.max_frame_bytes = 500000
    app.state.voice = VoiceService(inputs=app.state.inputs, output=ForbiddenOutput(), allow_woken_audio=False,
                                   max_audio_bytes=100000, allow_unwoken_pcm=True)
    app.state.robot.on_result = app.state.voice.on_result
    app.state.pcm_segments = PcmSegmentService(PcmSegmentConfig(preroll_ms=0), vad_factory=LabelledVad)
    original = window(1000, 10) + window(0, 16)
    headers = {"x-robot-token": "test-robot-token"}
    context = TrustedContext("test-customer", "robot", "test-binding")
    admitted = []
    async with live_app(app) as base:
        async with connect(base + "/api/voice/robot/ws?robot_id=test-robot",
                           additional_headers=headers, proxy=None) as commands:
            assert json.loads(await asyncio.wait_for(commands.recv(), 3))["state"] == "ready"
            await commands.send(json.dumps(HELLO))
            assert json.loads(await asyncio.wait_for(commands.recv(), 3))["channel"] == "command"
            async with connect(base + "/api/voice/robot/ws?robot_id=test-voice",
                               additional_headers=headers, proxy=None) as voice:
                assert json.loads(await asyncio.wait_for(voice.recv(), 3))["state"] == "ready"
                for sentence in range(2):
                    await voice.send(json.dumps({"type": "stats", "mic_frames": sentence}))
                    for chunk in (original[:1001], original[1001:]):
                        await voice.send(json.dumps({"type": "audio", "pcm_b64": base64.b64encode(chunk).decode()}))
                        await voice.send(json.dumps({"type": "stats", "audio_activated": True}))
                    thinking, segment, accepted, idle = [
                        json.loads(await asyncio.wait_for(voice.recv(), 3)) for _ in range(4)]
                    assert thinking["state"] == "thinking" and idle["state"] == "idle"
                    assert segment["received_chunks"] == 2 * (sentence + 1)
                    assert segment["received_pcm_bytes"] == len(original) * (sentence + 1)
                    with wave.open(io.BytesIO(base64.b64decode(segment["wav_b64"])), "rb") as wav:
                        assert wav.readframes(wav.getnframes()) == original
                    assert accepted["type"] == "pcm_task" and accepted["state"] == "RECEIVED"
                    assert accepted["segment_id"] == segment["segment_id"]
                    admitted.append(accepted["task_id"])
                assert len(set(admitted)) == 2
                assert app.state.inputs.asr.calls == 2
                assert await postgres_store.pool.fetchval("SELECT count(*) FROM ra_inputs") == 2
                assert await postgres_store.pool.fetchval("SELECT count(*) FROM ra_plans") == 0
                # Production admission stays asynchronous; drive the real scheduler explicitly in this test.
                await tasks.schedule_pending("test-deployment", task_id=admitted[0])
                await tasks.schedule_pending("test-deployment", task_id=admitted[1])
                first, second = [await tasks.get(context, task_id) for task_id in admitted]
                assert first.state == "READY" and first.plan_id
                assert second.state == "QUEUED" and second.plan_id is None
                assert await postgres_store.pool.fetchval("SELECT count(*) FROM ra_commands") == 0
                pong = await commands.ping()
                await asyncio.wait_for(pong, 3)
