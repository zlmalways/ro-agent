import pytest
from robot_agent.domain.models import PlanError
from robot_agent.protocol.dlv0_schema import parse_frame

BAD = [
    ("brain_skills", 7),
    ("brain_skills", [7]),
    ("brain_skills", [{}]),
    ("waypoint_names", "desk"),
    ("waypoint_names", [None]),
    ("map", 7),
    ("nav_server_ready", "true"),
    ("brain_server_ready", 1),
    ("busy", "false"),
    ("skill_server_ready", []),
    ("outbox", True),
    ("outbox", -1),
    ("waypoints", -2),
    ("waypoints", 1.2),
    ("preload", {}),
    ("brain_engine", False),
    ("brain_ckpt", []),
    ("brain_prompt_profile", 7),
    ("brain_preset", {}),
    ("executor", []),
]


@pytest.mark.parametrize("kind", ["hello", "readiness"])
@pytest.mark.parametrize("field,value", BAD)
def test_bad_report_rejected_at_both_entry_points(kind, field, value):
    with pytest.raises(PlanError):
        parse_frame({"type": kind, "robot_id": "test", "skills": ["move_to"], field: value})


@pytest.mark.parametrize("kind", ["hello", "readiness"])
def test_real_initial_unknown_values_and_skill_forms_remain_legal(kind):
    frame = {
        "type": kind,
        "robot_id": "test",
        "skills": ["move_to"],
        "map": None,
        "waypoints": None,
        "nav_server_ready": None,
        "brain_server_ready": None,
        "busy": False,
        "brain_skills": ["pick", {"skill_id": "place", "name": "放置"}],
        "waypoint_names": ["前台"],
    }
    assert parse_frame(frame)["nav_server_ready"] is None


def test_actual_robot_skill_catalog_shape_is_accepted():
    # deepleap-robot/dlv_protocol.py uses id/name/description/status.
    frame = {
        "type": "readiness",
        "brain_skills": [{"id": "pick", "name": "拿水", "description": "拿瓶水", "status": "ready"}],
    }
    assert parse_frame(frame) == frame
