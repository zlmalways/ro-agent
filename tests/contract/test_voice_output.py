import io
import wave

from robot_agent.adapters.voice_output import VoiceOutput, voice_frames
from robot_agent.domain.models import DependencyUnavailable
from robot_agent.domain.models import TaskView


def test_voice_round_ends_on_error():
    result = voice_frames(robot_id="r", transcript="", error="ASR_UNAVAILABLE")
    assert [(frame["type"], frame.get("state")) for frame in result] == [
        ("state", "thinking"), ("meta", None), ("done", None), ("state", "error"), ("state", "idle")]
    assert result[2]["play_secs"] == 0
    assert result[2]["robot_id"] == "r"
    assert result[2]["expects_reply"] is False


def test_voice_acceptance_is_not_physical_success():
    view = TaskView("task", 2, "READY", "", "plan", "", False)
    result = voice_frames(robot_id="r", transcript="test", view=view)
    assert result[-1] == {"type": "state", "state": "idle"}
    assert result[2]["text"] == "任务已受理，等待机器人执行。"
    assert result[2]["transcript"] == "test"
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
                                                   view=TaskView("task", 2, "READY", "", "plan", "", False))
    assert [frame["type"] for frame in result] == ["state", "meta", "audio", "done", "state"]
    assert result[-1]["state"] == "idle"
    assert result[-2]["play_secs"] == 0.1
    assert result[2]["content_type"] == "audio/wav"


async def test_synthesis_failure_still_returns_done_error_idle():
    class FailingFixture:
        async def synthesize(self, text):
            raise DependencyUnavailable("TTS_UNAVAILABLE")
    result = await VoiceOutput(FailingFixture()).render(robot_id="r", transcript="test",
                                                       view=TaskView("task", 2, "READY", "", "plan", "", False))
    assert result[-1]["state"] == "idle"
    assert result[-2]["state"] == "error"
    assert result[-3]["type"] == "done"
    assert not any(frame["type"] == "audio" for frame in result)
