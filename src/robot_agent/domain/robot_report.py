"""Value constraints shared by wire validation and stored-report diagnostics."""

from robot_agent.domain.models import DependencyUnavailable


class RobotReportError(DependencyUnavailable):
    def __init__(self, field):
        super().__init__("REPORT_INVALID:" + field)


BOOLEAN_FIELDS = frozenset(
    {"connected", "busy", "nav_server_ready", "brain_server_ready", "skill_server_ready"}
)
COUNT_FIELDS = frozenset({"outbox", "waypoints"})
STRING_FIELDS = frozenset(
    {
        "map",
        "executor",
        "preload",
        "brain_engine",
        "brain_ckpt",
        "brain_prompt_profile",
        "brain_preset",
        "busy_cmd_id",
        "runtime_id",
    }
)
LIST_FIELDS = frozenset({"waypoint_names", "skills", "capabilities"})
REPORT_FIELDS = BOOLEAN_FIELDS | COUNT_FIELDS | STRING_FIELDS | LIST_FIELDS | {"brain_skills"}


def brain_skill_id(value):
    return value.get("skill_id", value.get("id")) if isinstance(value, dict) else value


def validate_report_value(field, value):
    if field not in REPORT_FIELDS:
        return
    # Real devices use null while map/navigation/brain startup is unresolved.
    if value is None and field not in LIST_FIELDS | {"brain_skills", "connected", "busy"}:
        return
    if field in BOOLEAN_FIELDS:
        valid = type(value) is bool
    elif field in COUNT_FIELDS:
        valid = type(value) is int and value >= 0
    elif field in STRING_FIELDS:
        valid = isinstance(value, str)
    elif field in LIST_FIELDS:
        valid = isinstance(value, list) and all(isinstance(item, str) and item.strip() for item in value)
    else:
        valid = isinstance(value, list) and all(
            isinstance(brain_skill_id(item), str)
            and brain_skill_id(item).strip()
            and (
                not isinstance(item, dict)
                or all(isinstance(key, str) and isinstance(part, str) for key, part in item.items())
            )
            for item in value
        )
    if not valid:
        raise RobotReportError(field)
