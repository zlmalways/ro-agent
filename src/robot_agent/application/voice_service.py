"""Normalize provisioned robot voice input; task decisions stay in TaskService."""

import asyncio
from uuid import uuid4

from robot_agent.domain.models import ConflictError, DependencyUnavailable, NotFoundError, PlanError, TrustedContext


class VoiceService:
    def __init__(self, *, inputs, output, allow_woken_audio, max_audio_bytes, duplicate_policy=None,
                 allow_unwoken_pcm=False, wake_evaluation=None, submit_woken_pcm=False):
        self.inputs = inputs
        self.output = output
        self.allow_woken_audio = allow_woken_audio
        self.max_audio_bytes = max_audio_bytes
        self.duplicate_policy = duplicate_policy
        # Explicit deployment opt-in for the authorised PCM test; never inferred from client hints.
        self.allow_unwoken_pcm = allow_unwoken_pcm
        self.wake_evaluation = wake_evaluation
        self.submit_woken_pcm = submit_woken_pcm
        if submit_woken_pcm and (wake_evaluation is None or allow_unwoken_pcm):
            raise ValueError("wake-gated PCM submission requires wake evaluation without bypass")
        self.sessions = {}
        self.notifications = set()

    async def submit(self, context, robot_id, audio, request_key, *, woke):
        if self.wake_evaluation is not None:
            # The existing HTTP WAV entry shares this evaluation-only mode too.
            try:
                result = await self.wake_evaluation.evaluate_audio(context, audio, request_key or str(uuid4()))
            except (DependencyUnavailable, PlanError, ConflictError) as exc:
                result = {"type": "state", "state": "error", "message": str(exc)}
            return [{"type": "state", "state": "thinking"}, result, {"type": "state", "state": "idle"}]
        error, view = None, None
        if not self.allow_woken_audio or woke is not True:
            error = "VOICE_NOT_AWAKE"
        elif not request_key and self.duplicate_policy != "per_turn":
            error = "VOICE_DUPLICATE_POLICY_UNAVAILABLE"
        else:
            try:
                view = await self.inputs.audio(context, audio, "audio/wav", request_key or str(uuid4()),
                                                      wait_for_plan=True)
            except (DependencyUnavailable, PlanError) as exc:
                error = str(exc)
        frames = await self.output.render(robot_id=robot_id, transcript="", view=view, error=error)
        for frame in frames:
            if frame["type"] == "done":
                frame.update(member_id=None, guest_id=None, speaker_candidate=None)
        return frames

    def register(self, binding, send):
        key = (binding["customer_id"], binding["binding_id"])
        session = {"binding": binding, "send": send, "lock": asyncio.Lock(), "pcm": bytearray(), "pcm_admissions": {}}
        self.sessions[key] = session
        return session

    def unregister(self, session):
        session["pcm_admissions"].clear()
        key = (session["binding"]["customer_id"], session["binding"]["binding_id"])
        if self.sessions.get(key) is session:
            del self.sessions[key]

    def observe_pcm_activity(self, session, activity):
        """Apply worker-produced VAD facts on the event loop, never on a client hint."""
        if self.wake_evaluation is None:
            return
        binding = session["binding"]
        key = (binding["customer_id"], binding["binding_id"])
        if self.sessions.get(key) is not session:
            raise PlanError("VOICE_SESSION_REPLACED")
        admissions = session["pcm_admissions"]
        admissions[activity.segment_id] = self.wake_evaluation.wake.observe_speech(
            key, activity.segment_id, observed_at=activity.observed_at, started_at=activity.started_at,
            admission=admissions.get(activity.segment_id),
        )

    async def deliver(self, session, audio, *, woke):
        binding = session["binding"]
        if self.sessions.get((binding["customer_id"], binding["binding_id"])) is not session:
            return
        context = TrustedContext(binding["customer_id"], "robot", binding["binding_id"])
        async with session["lock"]:
            await session["send"]({"type": "state", "state": "thinking"})
            if self.wake_evaluation is not None:
                try:
                    result = await self.wake_evaluation.evaluate_audio(
                        context, audio, str(uuid4()),
                        is_current=lambda: self.sessions.get((binding["customer_id"], binding["binding_id"])) is session,
                    )
                    await session["send"](result)
                except (DependencyUnavailable, PlanError, ConflictError) as exc:
                    await session["send"]({"type": "state", "state": "error", "message": str(exc)})
                finally:
                    await session["send"]({"type": "state", "state": "idle"})
                return
            frames = await self.submit(context, binding["wire_robot_id"], audio, "", woke=woke)
            await self._send_tail(session, frames)

    async def deliver_pcm(self, session, segment):
        """Submit one complete PCM-derived WAV through the existing recorded input workflow."""
        if not self.allow_unwoken_pcm and self.wake_evaluation is None:
            raise PlanError("PCM_TASK_SUBMISSION_DISABLED")
        if segment.end_reason != "silence":
            raise PlanError("PCM_TOO_LONG")
        binding = session["binding"]
        key = (binding["customer_id"], binding["binding_id"])
        context = TrustedContext(binding["customer_id"], "robot", binding["binding_id"])
        async with session["lock"]:
            if self.sessions.get(key) is not session:
                raise PlanError("VOICE_SESSION_REPLACED")
            if self.wake_evaluation is not None:
                admission = session["pcm_admissions"].pop(segment.segment_id, None)
                if admission is None:
                    raise PlanError("PCM_SPEECH_ACTIVITY_MISSING")
                result = await self.wake_evaluation.evaluate_audio(
                    context, segment.wav_bytes, segment.segment_id, admission=admission,
                    is_current=lambda: self.sessions.get(key) is session,
                )
                if not self.submit_woken_pcm or result["action"] != "command":
                    return result
                await session["send"](result)
                view = await self.inputs.submit_transcribed_audio(
                    context, segment.segment_id, input_id=result["input_id"], text=result["command"],
                    is_current=lambda: self.sessions.get(key) is session,
                )
            else:
                view = await self.inputs.audio(context, segment.wav_bytes, "audio/wav", segment.segment_id)
        return {"type": "pcm_task", "segment_id": segment.segment_id, "request_key": segment.segment_id,
                "task_id": view.task_id, "state": view.state}

    async def _send_tail(self, session, frames):
        play_secs = 0
        for frame in frames[1:]:
            if frame["type"] == "done":
                play_secs = frame["play_secs"]
            if frame.get("state") == "idle" and play_secs:
                await asyncio.sleep(play_secs)
            await session["send"](frame)

    async def on_result(self, customer_id, binding_id, view):
        session = self.sessions.get((customer_id, binding_id))
        if session is None or view.state not in {"SUCCEEDED", "COMPLETED_WITH_SKIPS", "FAILED", "CANCELLED", "BLOCKED"}:
            return
        context = TrustedContext(customer_id, "robot", binding_id)
        try:
            # Only that device's own tasks may be spoken through its voice connection.
            current = await self.inputs.service.get(context, view.task_id)
        except NotFoundError:
            return
        task = asyncio.create_task(self._announce(session, current))
        self.notifications.add(task)
        task.add_done_callback(self.notifications.discard)

    async def _announce(self, session, view):
        try:
            async with session["lock"]:
                await session["send"]({"type": "state", "state": "thinking"})
                frames = await self.output.render(robot_id=session["binding"]["wire_robot_id"], transcript="", view=view)
                await self._send_tail(session, frames)
        except (OSError, RuntimeError):
            # The persistent TaskView/events remain queryable if voice delivery is lost.
            return

    async def close(self):
        if self.notifications:
            await asyncio.gather(*self.notifications, return_exceptions=True)
