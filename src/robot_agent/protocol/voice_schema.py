"""Voice receipts and committed task outcomes; no capture permission is granted."""

from robot_agent.domain.transitions import TERMINAL_TASK_STATES


def turn_finished(view):
    # A stopped turn must not resume, even when sent commands remain UNKNOWN.
    # This says nothing about physical stop or releasing the robot allocation.
    return view.state in TERMINAL_TASK_STATES or view.advancement_stopped


def task_frame(view, *, robot_id, transcript="", segment_id=None, capture_id=None):
    finished = turn_finished(view)
    kind = "done" if view.state == "REPLIED" else "task_finished" if finished else "task_state"
    frame = {
        "type": kind, "robot_id": robot_id, "task_id": view.task_id,
        "turn_id": view.turn_id or view.task_id, "version": view.version,
        "plan_id": view.plan_id, "state": view.state, "reason_code": view.reason_code,
        "turn_finished": finished, "advancement_stopped": view.advancement_stopped,
        "result_disputed": view.result_disputed, "error": view.error,
    }
    if segment_id is not None:
        frame["segment_id"] = segment_id
    if capture_id is not None:
        frame["capture_id"] = capture_id
    if kind == "done":
        text = next((item["content"] for item in reversed(view.messages)
                     if item.get("turn_id") == (view.turn_id or view.task_id)
                     and item.get("role") == "assistant" and item.get("message_kind") == "reply"), "")
        frame.update(kind="reply", text=text, reply=text, transcript=transcript,
                     is_question=False, expects_reply=False, play_secs=0, playback="none")
    elif view.state == "NEEDS_CLARIFICATION":
        frame.update(text=view.clarification, is_question=True)
    return frame


def input_error_frame(message, *, segment_id=None, capture_id=None):
    # No fabricated task outcome: admission may need to be reconciled by request key.
    frame = {"type": "state", "state": "error", "message": message, "phase": "voice_input"}
    if segment_id is not None:
        frame["segment_id"] = segment_id
    if capture_id is not None:
        frame["capture_id"] = capture_id
    return frame
