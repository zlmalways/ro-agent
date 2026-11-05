"""Command evidence reduction. No networking or persistence occurs here."""

from dataclasses import replace

from .models import ChangeSet, Command, RobotEvent, Task


def inflight_commands(task: Task) -> tuple[Command, ...]:
    """Already started commands without a definitive result; PREPARED is unsent."""
    return tuple(command for command in task.commands if command.dispatch_state != "PREPARED"
                 and command.outcome not in {"SUCCEEDED", "FAILED"})


def is_abandoned(task: Task) -> bool:
    """Policy-ended tasks accept old evidence without resuming execution."""
    return task.state == "CANCELLED" and task.reason_code in {"RESTART_TASK_LOST", "ROBOT_DISABLED"}


def check_transition(task: Task, command: Command | None, event: RobotEvent, *, current_generation=None) -> ChangeSet:
    if command is None or command.dispatch_state == "PREPARED":
        return ChangeSet(task)
    if event.customer_id != command.customer_id or event.robot_binding_id != command.robot_binding_id:
        return ChangeSet(task)
    generation = task.snapshot.generation if current_generation is None else current_generation
    if event.connection_generation != generation or event.cmd_id != command.command_id:
        return ChangeSet(task)
    if event.state not in {"pending", "running", "succeeded", "failed"}:
        return ChangeSet(task)
    original = task
    abandoned = is_abandoned(task)
    outcome = {"pending": "NONE", "running": "RUNNING", "succeeded": "SUCCEEDED", "failed": "FAILED"}[event.state]
    if command.outcome in {"SUCCEEDED", "FAILED"}:
        if outcome in {"SUCCEEDED", "FAILED"} and outcome != command.outcome:
            changed = replace(command, evidence_conflict=True, evidence_ids=command.evidence_ids + (event.event_id,))
            task = replace(task, result_disputed=True,
                           state=task.state if task.state in {"SUCCEEDED", "FAILED", "CANCELLED"} else "BLOCKED",
                           reason_code="EVIDENCE_CONFLICT")
        else:
            return ChangeSet(task)
    else:
        if command.outcome == "RESULT_UNKNOWN" and outcome in {"NONE", "RUNNING"}:
            return ChangeSet(task)
        if command.outcome == "RUNNING" and outcome == "NONE":
            return ChangeSet(task)
        changed = replace(command, outcome=outcome, evidence_ids=command.evidence_ids + (event.event_id,))
        if outcome == "SUCCEEDED":
            completed = task.completed + (command.step_id,)
            state = "SUCCEEDED" if len(completed) == len(task.plan.steps) else task.state
            reason = task.reason_code
            if reason == "RESULT_UNKNOWN":
                state, reason = ("SUCCEEDED" if len(completed) == len(task.plan.steps) else "RUNNING"), ""
            if task.cancel_requested or task.state == "CANCEL_REQUESTED":
                state = "CANCELLED"
            elif task.state == "BLOCKED" and reason and reason != "RESULT_UNKNOWN":
                state = "BLOCKED"
            task = replace(task, completed=completed, state=state, reason_code=reason)
        elif outcome == "FAILED":
            if task.cancel_requested or task.state == "CANCEL_REQUESTED":
                task = replace(task, state="CANCELLED", reason_code="")
            else:
                task = replace(task, state="BLOCKED", reason_code="ACTION_FAILED")
    if abandoned:
        # Late evidence belongs to the old command, never to a new execution.
        task = replace(task, state=original.state, reason_code=original.reason_code,
                       completed=original.completed, resume_phase=original.resume_phase)
    commands = tuple(changed if item.command_id == command.command_id else item for item in task.commands)
    return ChangeSet(replace(task, commands=commands, version=task.version + 1), changed, event)


def uncertain_execution(task: Task) -> ChangeSet:
    if task.state in {"SUCCEEDED", "FAILED", "CANCELLED"}:
        return ChangeSet(task)
    changed = tuple(replace(command, outcome="RESULT_UNKNOWN")
                    if command.dispatch_state in {"DISPATCHING", "SENT"} and command.outcome in {"NONE", "RUNNING"}
                    else command for command in task.commands)
    if changed == task.commands:
        return ChangeSet(task)
    # Preserve independent isolation/authorization reasons; uncertainty still stays on the command.
    reason = task.reason_code if task.reason_code and task.reason_code != "RESULT_UNKNOWN" else "RESULT_UNKNOWN"
    state = "CANCEL_REQUESTED" if task.cancel_requested else "BLOCKED"
    return ChangeSet(replace(task, commands=changed, state=state, reason_code=reason, version=task.version + 1))
