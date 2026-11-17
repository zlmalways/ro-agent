"""dlv/0 voice turn envelope; it never claims physical completion from model text."""

import base64
import io
import wave

from robot_agent.domain.models import DependencyUnavailable
from robot_agent.protocol.voice_schema import input_error_frame, task_frame


def fact_message(view):
    if view.result_disputed:
        return "机器人执行结果存在冲突，请在网页核对。"
    if view.error:
        return view.error["message"] + "原任务已停止推进，请在网页查看指令执行结果。"
    if view.state == "REPLIED":
        return next((message["content"] for message in reversed(view.messages)
                     if message.get("turn_id") == view.turn_id and message.get("role") == "assistant"
                     and message.get("message_kind") == "reply"), "本轮回复已保存，请在网页查看。")
    if view.state == "NEEDS_CLARIFICATION":
        return view.clarification + "请在网页回答这个问题。"
    if view.state == "WAITING_APPROVAL":
        return "任务计划需要人工批准，请在网页查看并批准。"
    if view.state == "SUCCEEDED":
        steps = "；".join(row.get("description", row["action"]) for row in view.result_summary if row["outcome"] == "SUCCEEDED")
        return "机器人报告任务已完成。" + steps
    if view.state == "COMPLETED_WITH_SKIPS":
        return "本轮执行已结束，包含由用户跳过的步骤。跳过的动作不计为成功。"
    if view.state in {"PAUSE_REQUESTED", "PAUSED"}:
        return "后续动作已暂停，请在网页查看当前动作的结果，并选择继续或结束本轮任务。"
    if view.state == "BLOCKED":
        return "原任务已停止推进，执行条件或结果需要核对。请在网页查看详情。"
    if view.state == "CANCEL_REQUESTED":
        return "已请求取消后续任务，仍在等待机器人反馈，尚未确认物理停止。"
    if view.state == "CANCELLED":
        return "后续任务已结束。已执行的动作仍保留在记录中。"
    if view.state == "FAILED":
        return "任务没有完成，请在网页查看原因。"
    return "任务已受理，等待机器人执行。"

def voice_frames(*, robot_id, transcript, view=None, error=None):
    frames = [{"type": "state", "state": "thinking"},
              {"type": "meta", "transcript": transcript, "awake": True}]
    if error:
        frames.append(input_error_frame(error))
    elif view is not None:
        frames.append(task_frame(view, robot_id=robot_id, transcript=transcript))
    return frames


class VoiceOutput:
    def __init__(self, tts):
        self.tts = tts

    async def render(self, *, robot_id, transcript, view=None, error=None):
        frames = voice_frames(robot_id=robot_id, transcript=transcript, view=view, error=error)
        # Missing TTS is the supported text-only deployment, not a voice failure.
        # Admission and intermediate states must never generate a reply-ending done.
        if error or view is None or view.state != "REPLIED" or self.tts is None:
            return frames
        reply = frames[-1]
        try:
            audio = await self.tts.synthesize(reply["text"])
            with wave.open(io.BytesIO(audio), "rb") as wav:
                seconds = wav.getnframes() / wav.getframerate()
            reply.update(play_secs=seconds, playback="audio")
            frames.insert(-1, {"type": "audio", "wav_b64": base64.b64encode(audio).decode(),
                               "content_type": "audio/wav", "task_id": reply["task_id"],
                               "turn_id": reply["turn_id"], "idx": 0, "played_chars": len(reply["text"])})
        except (DependencyUnavailable, wave.Error, EOFError):
            reply["playback_error"] = "TTS_UNAVAILABLE"
        return frames
