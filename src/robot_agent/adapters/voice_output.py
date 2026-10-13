"""dlv/0 voice turn envelope; it never claims physical completion from model text."""

import base64
import io
import wave

from robot_agent.domain.models import DependencyUnavailable


def fact_message(view):
    if view.result_disputed:
        return "机器人执行结果存在冲突，请在网页核对。"
    if view.state == "NEEDS_CLARIFICATION":
        return view.clarification + "请在网页回答这个问题。"
    if view.state == "WAITING_APPROVAL":
        return "任务计划需要人工批准，请在网页查看并批准。"
    if view.state == "SUCCEEDED":
        steps = "；".join(row.get("description", row["action"]) for row in view.result_summary if row["outcome"] == "SUCCEEDED")
        return "机器人报告任务已完成。" + steps
    if view.state == "BLOCKED":
        return "任务暂时暂停，执行条件或结果需要核对。请在网页查看详情。"
    if view.state == "CANCEL_REQUESTED":
        return "已请求取消后续任务，仍在等待机器人反馈，尚未确认物理停止。"
    if view.state == "CANCELLED":
        return "后续任务已结束。已执行的动作仍保留在记录中。"
    if view.state == "FAILED":
        return "任务没有完成，请在网页查看原因。"
    return "任务已受理，等待机器人执行。"

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


class VoiceOutput:
    def __init__(self, tts):
        self.tts = tts

    async def render(self, *, robot_id, transcript, view=None, error=None):
        frames = voice_frames(robot_id=robot_id, transcript=transcript, view=view, error=error)
        if view and not error:
            text = fact_message(view)
            frames[2].update(text=text, reply=text, expects_reply=False)
        if error:
            return frames
        try:
            if self.tts is None:
                raise DependencyUnavailable("TTS_UNAVAILABLE")
            audio = await self.tts.synthesize(frames[2]["text"])
            with wave.open(io.BytesIO(audio), "rb") as wav:
                seconds = wav.getnframes() / wav.getframerate()
            frames[2]["play_secs"] = seconds
            frames.insert(2, {"type": "audio", "wav_b64": base64.b64encode(audio).decode(), "content_type": "audio/wav",
                              "idx": 0, "played_chars": len(frames[2]["text"])})
        except (DependencyUnavailable, wave.Error, EOFError):
            frames.insert(-1, {"type": "state", "state": "error", "message": "TTS_UNAVAILABLE"})
        return frames
