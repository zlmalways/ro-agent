import io
import wave
from dataclasses import replace

import pytest

from robot_agent.adapters.voice_output import VoiceOutput, voice_frames
from robot_agent.domain.models import DependencyUnavailable
from robot_agent.domain.models import TaskView
from robot_agent.protocol.voice_schema import task_frame


def test_voice_round_ends_on_error():
    result = voice_frames(robot_id="r", transcript="", error="ASR_UNAVAILABLE")
    assert [(frame["type"], frame.get("state")) for frame in result] == [
        ("state", "thinking"), ("meta", None), ("state", "error")]
    assert result[-1]["message"] == "ASR_UNAVAILABLE"
    assert not any(frame["type"] in {"done", "task_finished"} for frame in result)


def test_voice_acceptance_is_not_physical_success():
    view = TaskView("task", 2, "READY", "", "plan", "", False)
    result = voice_frames(robot_id="r", transcript="test", view=view)
    assert result[-1]["type"] == "task_state"
    assert result[-1]["state"] == "READY"
    assert result[-1]["turn_finished"] is False
    assert not any(frame["type"] == "done" for frame in result)
    assert not any(frame["type"] == "audio" for frame in result)


class TTSFixture:
    async def synthesize(self, text):
        output = io.BytesIO()
        with wave.open(output, "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(16000)
            wav.writeframes(b"\x00\x00" * 1600)
        return output.getvalue()


async def test_spoken_response_contains_audio_and_closes_round():
    result = await VoiceOutput(TTSFixture()).render(robot_id="r", transcript="test",
                                                   view=reply_view())
    assert [frame["type"] for frame in result] == ["state", "meta", "audio", "done"]
    assert result[-1]["state"] == "REPLIED"
    assert result[-1]["play_secs"] == 0.1
    assert result[-1]["playback"] == "audio"
    assert result[2]["content_type"] == "audio/wav"


async def test_synthesis_failure_still_returns_text_done():
    class FailingFixture:
        async def synthesize(self, text):
            raise DependencyUnavailable("TTS_UNAVAILABLE")
    result = await VoiceOutput(FailingFixture()).render(robot_id="r", transcript="test",
                                                       view=reply_view())
    assert result[-1]["type"] == "done"
    assert result[-1]["playback_error"] == "TTS_UNAVAILABLE"
    assert result[-1]["playback"] == "none"
    assert not any(frame["type"] == "audio" for frame in result)


def reply_view():
    # Protocol sample only, never evidence of an actual model/task execution.
    return TaskView("conversation", 3, "REPLIED", "", None, "", False, turn_id="turn-1", messages=(
        {"turn_id": "turn-1", "role": "assistant", "message_kind": "reply", "content": "你好。"},
        {"turn_id": "turn-2", "role": "assistant", "message_kind": "reply", "content": "另一轮回复。"},
    ))


async def test_text_only_reply_does_not_require_tts_or_emit_idle():
    result = await VoiceOutput(None).render(robot_id="r", transcript="你好", view=reply_view())
    assert [frame["type"] for frame in result] == ["state", "meta", "done"]
    assert result[-1]["text"] == "你好。"
    assert result[-1]["turn_id"] == "turn-1"
    assert result[-1]["playback"] == "none" and result[-1]["play_secs"] == 0


@pytest.mark.parametrize("state", ["RECEIVED", "PLANNING", "QUEUED", "READY", "RUNNING",
                                   "NEEDS_CLARIFICATION", "WAITING_APPROVAL", "PAUSED", "CANCEL_REQUESTED"])
def test_pending_task_is_never_a_completion(state):
    frame = task_frame(replace(reply_view(), state=state), robot_id="r")
    assert frame["type"] == "task_state" and frame["turn_finished"] is False


@pytest.mark.parametrize("state", ["SUCCEEDED", "COMPLETED_WITH_SKIPS", "FAILED", "CANCELLED"])
def test_terminal_task_retains_actual_outcome(state):
    frame = task_frame(replace(reply_view(), state=state), robot_id="r", segment_id="s", capture_id="c")
    assert frame["type"] == "task_finished" and frame["turn_finished"] is True
    assert frame["state"] == state
    assert frame["segment_id"] == "s" and frame["capture_id"] == "c"


def test_stopped_blocked_turn_does_not_claim_physical_success():
    view = replace(reply_view(), state="BLOCKED", reason_code="RESULT_UNKNOWN", advancement_stopped=True,
                   error={"code": "RESULT_UNKNOWN", "execution_status": "UNKNOWN"})
    frame = task_frame(view, robot_id="r")
    assert frame["type"] == "task_finished" and frame["turn_finished"] is True
    assert frame["state"] == "BLOCKED" and frame["error"]["execution_status"] == "UNKNOWN"
    assert "physical_stopped" not in frame
