"""Opt-in audio-to-wake evaluation. Never submits tasks or renders speech."""

from dataclasses import asdict
import logging
from time import perf_counter

from robot_agent.application.voice_wake_service import FollowupAdmission
from robot_agent.domain.models import ConflictError, PlanError


log = logging.getLogger(__name__)


class VoiceWakeEvaluation:
    def __init__(self, inputs, wake):
        self.inputs = inputs
        self.wake = wake

    async def evaluate_audio(self, context, audio, request_key, *, is_current=None, admission=None):
        started = perf_counter()
        key = (context.customer_id, context.actor_id)
        if is_current is not None and not is_current():
            raise PlanError("VOICE_SESSION_REPLACED")
        if admission is None:
            # Whole-WAV uploads have no live VAD evidence. Capture eligibility
            # before ASR, without treating bytes/client woke flags as speech.
            admission = self.wake.capture_input(key, request_key)
        elif (not isinstance(admission, FollowupAdmission) or admission.key != key
              or admission.segment_id != request_key):
            raise PlanError("VOICE_ADMISSION_MISMATCH")
        transcript = await self.inputs.transcribe_audio(context, audio, "audio/wav", request_key)
        if is_current is not None and not is_current():
            raise PlanError("VOICE_SESSION_REPLACED")
        if transcript.result_task_id is not None:
            raise ConflictError("wake evaluation input already belongs to a task")
        if not transcript.text:
            raise PlanError("ASR_EMPTY")
        wake_started = perf_counter()
        decision = self.wake.evaluate(key, transcript.text, admission=admission)
        timings = {
            "asr": round(transcript.asr_ms, 3),
            "wake": round((perf_counter() - wake_started) * 1000, 3),
            "total": round((perf_counter() - started) * 1000, 3),
        }
        result = {
            "type": "pcm_wake", "segment_id": request_key, "input_id": transcript.input_id,
            "transcript": transcript.text, "provider_ref": transcript.provider_ref,
            "asr_reused": transcript.reused, **asdict(decision), "timings_ms": timings,
            "followup": {name: value for name, value in asdict(admission).items() if name != "key"},
        }
        log.info("voice_wake input=%s segment=%s action=%s reason=%s asr_ms=%s wake_ms=%s total_ms=%s",
                 transcript.input_id, request_key, decision.action, decision.reason,
                 timings["asr"], timings["wake"], timings["total"])
        return result
