"""dlv/0 voice turn envelope; it never claims physical completion from model text."""


def voice_frames(*, robot_id, transcript, view=None, error=None):
    question = bool(view and view.state == "NEEDS_CLARIFICATION")
    text = ("语音任务处理失败：" + error) if error else (
        view.clarification if question else "任务已受理，等待机器人执行。")
    frames = [{"type": "state", "state": "thinking"}, {"type": "meta", "transcript": transcript, "awake": True},
              {"type": "done", "text": text, "reply": text, "transcript": transcript, "robot_id": robot_id,
               "is_question": question, "expects_reply": question, "play_secs": 0}]
    if error:
        frames.append({"type": "state", "state": "error", "message": error})
    frames.append({"type": "state", "state": "idle"})
    return frames
