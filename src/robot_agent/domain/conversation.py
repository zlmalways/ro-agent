"""Conversation controls derived from persisted execution facts."""
from .transitions import (EXECUTION_PAUSE_STATES, TERMINAL_TASK_STATES, can_resume_execution,
                          inflight_commands, remaining_steps)

ENDED = TERMINAL_TASK_STATES


def conversation_interaction(task):
    if task is None:
        phase = "idle"
    elif task.state in EXECUTION_PAUSE_STATES and not task.advancement_stopped and not task.result_disputed:
        phase = "execution_paused" if task.state == "PAUSED" else "execution_stopping"
    elif (inflight_commands(task) or task.result_disputed
          or task.reason_code == "UNTRACKED_EXECUTION"):
        phase = "stopping" if task.cancel_requested else "executing"
    elif task.state in ENDED or task.advancement_stopped:
        phase = "idle"
    elif task.state == "NEEDS_CLARIFICATION":
        phase = "idle"
    elif task.state == "WAITING_APPROVAL":
        phase = "approval"
    elif task.state == "CANCEL_REQUESTED":
        # A late definitive receipt can close the physical action while the
        # original error/state remains latched for auditing.
        phase = "idle" if task.commands else "stopping"
    elif task.state in {"RECEIVED", "PLANNING"} or (task.state == "QUEUED" and task.plan is None):
        phase = "planning"
    elif task.state in {"READY", "QUEUED", "RUNNING"}:
        phase = "executing"
    else:
        phase = "unavailable"
    cancellable = (task is not None and task.state not in ENDED
                   and not task.cancel_requested and not task.cancel_result)
    resumable = task is not None and can_resume_execution(task)
    following = (tuple(step for step in remaining_steps(task) if step.step_id != task.pause_step_id)
                 if task is not None and task.state in EXECUTION_PAUSE_STATES else ())
    return {
        "phase": phase,
        "can_message": phase == "idle",
        "can_pause": phase == "planning" and cancellable,
        "can_decide": phase == "approval",
        "can_stop": phase == "executing" and cancellable,
        "can_stop_execution": bool(task and task.plan and task.snapshot and cancellable
                                   and not task.advancement_stopped and not task.result_disputed
                                   and task.state in {"READY", "RUNNING"}),
        "can_resume_execution": resumable,
        "can_end_execution": bool(task and task.state == "PAUSED" and cancellable),
        "resume_next_step_id": following[0].step_id if following else None,
        "resume_skips_current": bool(resumable and task.pause_step_id and task.pause_step_id not in task.completed),
    }
