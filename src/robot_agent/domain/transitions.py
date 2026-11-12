"""Command evidence reduction. No networking or persistence occurs here."""

from dataclasses import replace

from .models import ChangeSet, Command, RobotEvent, Task
from .errors import stop_task


TERMINAL_TASK_STATES = frozenset({"REPLIED", "SUCCEEDED", "COMPLETED_WITH_SKIPS", "FAILED", "CANCELLED"})
EXECUTION_PAUSE_STATES = frozenset({"PAUSE_REQUESTED", "PAUSED"})


def remaining_steps(task):
    resolved = set(task.completed) | set(task.skipped_steps)
    return tuple(step for step in task.plan.steps if step.step_id not in resolved) if task.plan else ()


def completed_state(task):
    return "COMPLETED_WITH_SKIPS" if task.skipped_steps else "SUCCEEDED"


def can_resume_execution(task):
    if (task.state != "PAUSED" or not task.plan or not task.snapshot or task.cancel_requested or task.advancement_stopped
            or task.result_disputed or inflight_commands(task) or not task.pause_result
            or task.pause_result.get("status") == "PENDING"):
        return False
    if not task.pause_command_id:
        return not task.pause_step_id
    target = next((item for item in task.commands if item.command_id == task.pause_command_id), None)
    return bool(target and target.step_id == task.pause_step_id and not target.evidence_conflict
                and (target.outcome in {"SUCCEEDED", "FAILED"} or target.dispatch_state == "NOT_SENT"))


def inflight_commands(task: Task) -> tuple[Command, ...]:
    """Already started commands without a definitive result; PREPARED is unsent."""
    return tuple(command for command in task.commands if command.dispatch_state not in {"PREPARED", "NOT_SENT"}
                 and command.outcome not in {"SUCCEEDED", "FAILED"})


def is_abandoned(task: Task) -> bool:
    """Policy-ended tasks accept old evidence without resuming execution."""
    return task.state == "CANCELLED" and task.reason_code in {"RESTART_TASK_LOST", "ROBOT_DISABLED"}


def check_transition(task: Task, command: Command | None, event: RobotEvent, *, current_generation=None) -> ChangeSet:
    if command is None or command.dispatch_state in {"PREPARED", "NOT_SENT"}:
        return ChangeSet(task)
    if event.customer_id != command.customer_id or event.robot_binding_id != command.robot_binding_id:
        return ChangeSet(task)
    generation = task.snapshot.generation if current_generation is None else current_generation
    if event.connection_generation != generation or event.cmd_id != command.command_id:
        return ChangeSet(task)
    if event.state not in {"pending", "running", "succeeded", "failed"}:
        return ChangeSet(task)
    original = task
    stopped = task.advancement_stopped or is_abandoned(task)
    outcome = {"pending": "NONE", "running": "RUNNING", "succeeded": "SUCCEEDED", "failed": "FAILED"}[event.state]
    if command.outcome in {"SUCCEEDED", "FAILED"}:
        if outcome in {"SUCCEEDED", "FAILED"} and outcome != command.outcome:
            changed = replace(command, evidence_conflict=True, evidence_ids=command.evidence_ids + (event.event_id,))
            task = replace(task, result_disputed=True,
                           state=task.state if task.state in TERMINAL_TASK_STATES else "BLOCKED",
                           reason_code="EVIDENCE_CONFLICT")
            task = stop_task(task, "EVIDENCE_CONFLICT", source="execution", phase="feedback", command=command)
        else:
            return ChangeSet(task)
    else:
        if command.outcome == "RESULT_UNKNOWN" and outcome in {"NONE", "RUNNING"}:
            return ChangeSet(task)
        if command.outcome == "RUNNING" and outcome == "NONE":
            return ChangeSet(task)
        changed = replace(command, outcome=outcome, evidence_ids=command.evidence_ids + (event.event_id,))
        pausing = (task.state in EXECUTION_PAUSE_STATES and command.command_id == task.pause_command_id
                   and not task.cancel_requested)
        if outcome == "SUCCEEDED" and not stopped:
            completed = task.completed + (command.step_id,)
            state = completed_state(task) if not remaining_steps(replace(task, completed=completed)) else task.state
            reason = task.reason_code
            if pausing:
                state = "PAUSED"
            elif task.cancel_requested or task.state == "CANCEL_REQUESTED":
                state = "CANCELLED"
            elif task.state == "BLOCKED" and reason and reason != "RESULT_UNKNOWN":
                state = "BLOCKED"
            task = replace(task, completed=completed, state=state, reason_code=reason)
        elif outcome == "FAILED" and not stopped:
            if pausing:
                task = replace(task, state="PAUSED", reason_code="")
            elif task.cancel_requested or task.state == "CANCEL_REQUESTED":
                task = replace(task, state="CANCELLED", reason_code="")
            else:
                task = replace(task, state="BLOCKED", reason_code="ACTION_FAILED")
                task = stop_task(task, "ACTION_FAILED", source="execution", phase="feedback", command=command)
    if stopped:
        # Late evidence belongs to the old command, never to a new execution.
        task = replace(task, state=original.state, reason_code=original.reason_code,
                       completed=original.completed, resume_phase=original.resume_phase)
    commands = tuple(changed if item.command_id == command.command_id else item for item in task.commands)
    return ChangeSet(replace(task, commands=commands, version=task.version + 1), changed, event)


def uncertain_execution(task: Task, *, reason="RESULT_UNKNOWN", phase="feedback") -> ChangeSet:
    if task.state in TERMINAL_TASK_STATES:
        return ChangeSet(task)
    changed = tuple(replace(command, outcome="RESULT_UNKNOWN")
                    if command.dispatch_state in {"DISPATCHING", "SENT"} and command.outcome in {"NONE", "RUNNING"}
                    else command for command in task.commands)
    if changed == task.commands:
        return ChangeSet(task)
    if task.state in EXECUTION_PAUSE_STATES and not task.cancel_requested:
        # A timeout/disconnect is not an end receipt. Keep the pause and its
        # allocation until a definitive result arrives; do not enable resume.
        return ChangeSet(replace(task, commands=changed, state="PAUSE_REQUESTED", version=task.version + 1))
    # Preserve independent isolation/authorization reasons; uncertainty still stays on the command.
    task_reason = task.reason_code if task.reason_code and task.reason_code != "RESULT_UNKNOWN" else reason
    state = "CANCEL_REQUESTED" if task.cancel_requested else "BLOCKED"
    updated = replace(task, commands=changed, state=state, reason_code=task_reason, version=task.version + 1)
    command = next((item for item in changed if item.outcome == "RESULT_UNKNOWN"), None)
    return ChangeSet(stop_task(updated, reason, source="transport" if phase == "write" else "execution",
                               phase=phase, command=command))
