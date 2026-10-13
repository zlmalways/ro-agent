"""Selected dlv/0 contract from deepleap_agent@73918bee; see docs/protocol-provenance.md."""

from robot_agent.domain.models import PlanError


PROTOCOL = "dlv/0"
STATUS_STATES = frozenset({"pending", "running", "succeeded", "failed"})
READINESS_FIELDS = (
    "executor", "skill_server_ready", "busy", "outbox", "map", "waypoints", "waypoint_names",
    "preload", "nav_server_ready", "brain_server_ready", "brain_engine", "brain_ckpt",
    "brain_prompt_profile", "brain_preset", "brain_skills",
)
TELEMETRY_FIELDS = ("battery", "location", "observed_at")


def ready_frame(robot_id, *, command_channel=False):
    return {"type": "state", "state": "ready", **({"channel": "command"} if command_channel else {"protocol": PROTOCOL}),
            "robot_id": robot_id}


def command_frame(command):
    return {"type": "command", "skill": command.wire_skill, "args": dict(command.wire_args), "cmd_id": command.command_id}


def cancel_frame(command_id):
    return {"type": "cancel", "cmd_id": command_id}


def parse_frame(value):
    if not isinstance(value, dict):
        raise PlanError("robot frame must be an object")
    kind = value.get("type")
    if kind == "hello":
        if not isinstance(value.get("robot_id"), str) or not value["robot_id"]:
            raise PlanError("hello requires robot ID")
        for key in ("skills", "capabilities"):
            if key in value and (not isinstance(value[key], list) or any(not isinstance(item, str) for item in value[key])):
                raise PlanError("invalid hello list")
        for key in ("busy_cmd_id", "runtime_id", "executor"):
            if key in value and not isinstance(value[key], str):
                raise PlanError("invalid hello identifier")
        fields = ("robot_id", "skills", "capabilities", "busy_cmd_id", "runtime_id", "executor") + READINESS_FIELDS
    elif kind == "cmd_status":
        if not isinstance(value.get("cmd_id"), str) or not value["cmd_id"] or value.get("state") not in STATUS_STATES:
            raise PlanError("invalid cmd_status")
        fields = ("cmd_id", "state", "detail", "error_code")
    elif kind == "readiness":
        fields = READINESS_FIELDS
    elif kind == "telemetry":
        fields = TELEMETRY_FIELDS
    else:
        raise PlanError("unsupported robot frame")
    return {"type": kind, **{key: value[key] for key in fields if key in value}}
