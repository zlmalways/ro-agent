from robot_agent.adapters.voice_output import voice_frames
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
