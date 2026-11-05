"""Real PostgreSQL/InputService/TaskService with explicitly simulated VAD and ASR."""
import pytest

from robot_agent.application.input_service import InputService
from robot_agent.application.pcm_segment_service import PcmSegmentService
from robot_agent.application.voice_service import VoiceService
from robot_agent.config import PcmSegmentConfig
from tests.api.test_pcm_tasks import ForbiddenOutput
from tests.api.test_robot_pcm import LabelledVad, window
from tests.api.test_task_audio import ASRFixture


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
    assert first == second and first["state"] == "READY"
    assert first["request_key"] == first["segment_id"] == segment.segment_id
    assert asr.calls == 1 and robot.sent == []
    assert await postgres_store.pool.fetchval("SELECT count(*) FROM ra_tasks") == 1
    row = await postgres_store.pool.fetchrow(
        "SELECT key,normalized_text,provider_ref,state,result_task_id FROM ra_inputs")
    assert tuple(row) == (segment.segment_id, "test: fetch cup", "test-asr-1", "COMPLETE", first["task_id"])
    task = await postgres_store.pool.fetchrow("SELECT actor_kind,actor_id FROM ra_tasks")
    assert tuple(task) == ("robot", "test-binding")
