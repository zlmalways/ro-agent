"""Domain data contracts independent of HTTP, SDKs, and persistence."""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping


class PlanError(ValueError):
    pass


class ConflictError(ValueError):
    pass


class NotFoundError(LookupError):
    pass


@dataclass(frozen=True)
class TrustedContext:
    customer_id: str
    actor_kind: str
    actor_id: str


@dataclass(frozen=True)
class TaskInput:
    request_id: str
    channel: str
    text: str
    source_ref: str


@dataclass(frozen=True)
class CandidateStep:
    step_id: str
    action: str
    args: Mapping[str, Any]
    depends_on: tuple[str, ...]


@dataclass(frozen=True)
class CandidatePlan:
    outcome: str
    steps: tuple[CandidateStep, ...] = ()
    question: str = ""
    required_slots: tuple[str, ...] = ()
    reason: str = ""

    @classmethod
    def from_dict(cls, value: dict) -> "CandidatePlan":
        allowed = {"outcome", "steps", "question", "required_slots", "reason"}
        if not isinstance(value, dict) or set(value) - allowed:
            raise PlanError("invalid candidate fields")
        outcome = value.get("outcome")
        if outcome not in {"plan", "clarify", "unsupported"}:
            raise PlanError("invalid candidate outcome")
        raw_steps = value.get("steps", [])
        if not isinstance(raw_steps, list):
            raise PlanError("invalid steps")
        if outcome != "plan" and raw_steps:
            raise PlanError("clarify/unsupported cannot contain steps")
        if outcome == "plan" and not raw_steps:
            raise PlanError("plan requires steps")
        steps = []
        for item in raw_steps:
            if not isinstance(item, dict) or set(item) != {"step_id", "action", "args", "depends_on"}:
                raise PlanError("invalid step fields")
            if not isinstance(item["step_id"], str) or not item["step_id"]:
                raise PlanError("invalid step id")
            if not isinstance(item["action"], str) or not isinstance(item["args"], dict):
                raise PlanError("invalid action/args")
            if not isinstance(item["depends_on"], list) or any(not isinstance(dep, str) for dep in item["depends_on"]):
                raise PlanError("invalid dependencies")
            steps.append(CandidateStep(item["step_id"], item["action"], dict(item["args"]), tuple(item["depends_on"])))
        question = value.get("question", "")
        if outcome == "clarify" and (not isinstance(question, str) or not question.strip()):
            raise PlanError("clarify requires question")
        slots = value.get("required_slots", [])
        if not isinstance(slots, list) or any(not isinstance(slot, str) for slot in slots):
            raise PlanError("invalid required slots")
        return cls(outcome, tuple(steps), question, tuple(slots), str(value.get("reason", "")))


@dataclass(frozen=True)
class ContextSnapshot:
    customer_id: str
    robot_binding_id: str
    generation: int
    skills: frozenset[str]
    waypoints: Mapping[str, str]
    brain_skills: Mapping[str, str]
    readiness: Mapping[str, Any]
    snapshot_id: str
    allowed_actions: frozenset[str]


@dataclass(frozen=True)
class ActionCatalog:
    required: Mapping[str, tuple[str, ...]]


@dataclass(frozen=True)
class PlanStep:
    step_id: str
    action: str
    args: Mapping[str, Any]
    depends_on: tuple[str, ...]
    source_refs: tuple[str, ...]


@dataclass(frozen=True)
class ValidationResult:
    steps: tuple[PlanStep, ...]
    snapshot_id: str


@dataclass(frozen=True)
class Plan:
    plan_id: str
    revision: int
    steps: tuple[PlanStep, ...]
    snapshot_id: str


@dataclass(frozen=True)
class Command:
    command_id: str
    task_id: str
    customer_id: str
    unit_id: str
    plan_id: str
    step_id: str
    robot_binding_id: str
    wire_skill: str
    wire_args: Mapping[str, Any]
    dispatch_generation: int
    dispatch_state: str = "PREPARED"
    outcome: str = "NONE"
    evidence_conflict: bool = False
    evidence_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class RobotEvent:
    event_id: str
    customer_id: str
    robot_binding_id: str
    connection_generation: int
    cmd_id: str
    state: str
    received_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    details: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Decision:
    decision_id: str
    kind: str
    result: str
    rule_or_policy_version: str
    evidence_refs: tuple[str, ...] = ()
    detail: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class TaskEvent:
    event_type: str
    public_payload: Mapping[str, Any]


@dataclass(frozen=True)
class SendObservation:
    sent: bool


@dataclass(frozen=True)
class Task:
    task_id: str
    customer_id: str
    actor_kind: str
    actor_id: str
    goal_text: str
    state: str = "RECEIVED"
    reason_code: str = ""
    version: int = 1
    plan: Plan | None = None
    snapshot: ContextSnapshot | None = None
    unit_id: str = ""
    commands: tuple[Command, ...] = ()
    completed: tuple[str, ...] = ()
    clarification: str = ""
    result_disputed: bool = False


@dataclass(frozen=True)
class ChangeSet:
    task: Task
    command: Command | None = None
    event: RobotEvent | None = None
    decisions: tuple[Decision, ...] = ()


@dataclass(frozen=True)
class TaskView:
    task_id: str
    version: int
    state: str
    reason_code: str
    plan_id: str | None
    clarification: str
    result_disputed: bool
    result_summary: tuple[dict, ...] = field(default_factory=tuple)
