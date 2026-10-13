import pytest

from robot_agent.domain.models import Command, PlanError
from robot_agent.protocol.dlv0_schema import cancel_frame, command_frame, parse_frame, ready_frame


def test_dlv0_frames_unchanged():
    command = Command("test-cmd", "task", "customer", "unit", "plan", "s1", "binding",
                      "move_to", {"location": "table"}, 1)
    assert ready_frame("test-robot") == {"type": "state", "state": "ready", "protocol": "dlv/0", "robot_id": "test-robot"}
    assert ready_frame("test-robot", command_channel=True) == {"type": "state", "state": "ready", "channel": "command", "robot_id": "test-robot"}
    assert command_frame(command) == {"type": "command", "skill": "move_to", "args": {"location": "table"}, "cmd_id": "test-cmd"}
    assert cancel_frame("test-cmd") == {"type": "cancel", "cmd_id": "test-cmd"}
    hello = {"type": "hello", "robot_id": "test-robot", "skills": ["move_to", "vla_skill"],
             "capabilities": ["locomotion", "manipulation"], "busy_cmd_id": "", "runtime_id": "runtime-1"}
    assert parse_frame(hello) == hello
    assert parse_frame({"type": "cmd_status", "cmd_id": "test-cmd", "state": "succeeded", "detail": "arrived"}) == {
        "type": "cmd_status", "cmd_id": "test-cmd", "state": "succeeded", "detail": "arrived"}


@pytest.mark.parametrize("frame", [
    {"type": "cmd_status", "cmd_id": "c", "state": "accepted"},
    {"type": "accepted", "cmd_id": "c"}, {"type": "progress", "cmd_id": "c"},
    {"type": "cmd_status", "cmd_id": 4, "state": "succeeded"},
    {"type": "hello", "robot_id": "r", "skills": "move_to"},
])
def test_rejects_invalid_frames_and_v1(frame):
    with pytest.raises(PlanError):
        parse_frame(frame)


def test_reserved_fields_do_not_enter_robot_evidence():
    assert parse_frame({"type": "cmd_status", "cmd_id": "c", "state": "running", "customer_id": "forged",
                        "token": "secret", "source_refs": ["forged"]}) == {
        "type": "cmd_status", "cmd_id": "c", "state": "running"}
