"""Normalize provisioned robot voice input; task decisions stay in TaskService."""

import asyncio
import logging
from uuid import uuid4

from robot_agent.domain.models import ConflictError, DependencyUnavailable, NotFoundError, PlanError, TrustedContext
from robot_agent.protocol.voice_schema import input_error_frame, task_frame, turn_finished


log = logging.getLogger(__name__)


class VoiceService:
    def __init__(self, *, inputs, output, allow_woken_audio, max_audio_bytes, duplicate_policy=None,
                 allow_unwoken_pcm=False, wake_evaluation=None, submit_woken_pcm=False):
        self.inputs = inputs
        self.output = output
        self.allow_woken_audio = allow_woken_audio
        self.max_audio_bytes = max_audio_bytes
        self.duplicate_policy = duplicate_policy
        # Deployment decides whether PCM needs platform wake evaluation; never a client flag.
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
            return [{"type": "state", "state": "thinking"}, result]
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
        previous = self.sessions.get(key)
        if previous is not None:
            self.unregister(previous)
        session = {"binding": binding, "send": send, "lock": asyncio.Lock(), "pcm": bytearray(),
                   "pcm_admissions": {}, "task_notifications": {}}
        self.sessions[key] = session
        return session

    def reset_pcm_activity(self, session):
        """Discard unfinished utterance eligibility at a PCM capture boundary."""
        binding = session["binding"]
        if self.sessions.get((binding["customer_id"], binding["binding_id"])) is not session:
            raise PlanError("VOICE_SESSION_REPLACED")
        session["pcm_admissions"].clear()

    def unregister(self, session):
        session["pcm_admissions"].clear()
        for notification in session["task_notifications"].values():
            notification.cancel()
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

    async def deliver(self, session, audio, *, woke, capture_id=None):
        binding = session["binding"]
        if self.sessions.get((binding["customer_id"], binding["binding_id"])) is not session:
            return
        context = TrustedContext(binding["customer_id"], "robot", binding["binding_id"])
        async with session["lock"]:
            await session["send"]({"type": "state", "state": "thinking",
                                   **({"capture_id": capture_id} if capture_id is not None else {})})
            if self.wake_evaluation is not None:
                try:
                    result = await self.wake_evaluation.evaluate_audio(
                        context, audio, str(uuid4()),
                        is_current=lambda: self.sessions.get((binding["customer_id"], binding["binding_id"])) is session,
                    )
                    await session["send"](result)
                except (DependencyUnavailable, PlanError, ConflictError) as exc:
                    await session["send"]({"type": "state", "state": "error", "message": str(exc)})
                return
            frames = await self.submit(context, binding["wire_robot_id"], audio, "", woke=woke)
            for frame in frames[1:]:
                if capture_id is not None:
                    frame = {**frame, "capture_id": capture_id}
                await session["send"](frame)
                if frame["type"] == "task_state":
                    self.watch_task(session, frame)

    async def deliver_text(self, session, text, *, request_key, conversation_id=None):
        """Accept text from a trusted upstream voice model on the voice socket."""
        binding = session["binding"]
        if self.sessions.get((binding["customer_id"], binding["binding_id"])) is not session:
            return
        context = TrustedContext(binding["customer_id"], "robot", binding["binding_id"])
        async with session["lock"]:
            view = await self.inputs.submit_text(context, request_key, text,
                                                 conversation_id=conversation_id)
        receipt = {"type": "text_task", "request_key": request_key,
                   "task_id": view.task_id, "turn_id": view.turn_id or view.task_id,
                   "version": view.version, "state": view.state}
        await session["send"](receipt)
        self.watch_task(session, receipt)
        return receipt

    async def deliver_pcm(self, session, segment, *, capture_id=None):
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
                    return {**result, **({"capture_id": capture_id} if capture_id is not None else {})}
                await session["send"]({**result, **({"capture_id": capture_id} if capture_id is not None else {})})
                view = await self.inputs.submit_transcribed_audio(
                    context, segment.segment_id, input_id=result["input_id"], text=result["command"],
                    is_current=lambda: self.sessions.get(key) is session,
                )
            else:
                view = await self.inputs.audio(context, segment.wav_bytes, "audio/wav", segment.segment_id)
        receipt = {"type": "pcm_task", "segment_id": segment.segment_id, "request_key": segment.segment_id,
                   "task_id": view.task_id, "turn_id": view.turn_id or view.task_id,
                   "version": view.version, "state": view.state}
        if capture_id is not None:
            receipt["capture_id"] = capture_id
        return receipt

    async def task_notifications(self, context, receipt, robot_id):
        """Project committed facts for one turn, including planning-only replies.

        Poll only while this connection is waiting for the turn. Reads use the
        public task boundary and cannot observe an uncommitted status or follow
        a newer turn in the same conversation. No task or allocation is mutated.
        """
        task_id, turn_id = receipt["task_id"], receipt["turn_id"]
        correlation = {key: receipt[key] for key in ("segment_id", "capture_id") if key in receipt}
        previous_version = None
        read_failed = False
        while True:
            try:
                view = await self.inputs.service.get_turn(context, task_id, turn_id)
            except (NotFoundError, PermissionError):
                yield {**input_error_frame("TASK_STATUS_UNAVAILABLE", **correlation),
                       "phase": "task_notification", "task_id": task_id, "turn_id": turn_id}
                return
            except Exception as exc:
                # A read failure is not a task completion. Keep the subscription
                # until the connection closes or the same durable turn is readable.
                if not read_failed:
                    log.error("voice task status read failed task=%s turn=%s error_type=%s",
                              task_id, turn_id, type(exc).__name__)
                    code = ("DATABASE_UNAVAILABLE"
                            if not getattr(self.inputs.service.store, "write_available", True)
                            else "TASK_STATUS_UNAVAILABLE")
                    yield {**input_error_frame(code, **correlation),
                           "phase": "task_notification", "task_id": task_id, "turn_id": turn_id}
                read_failed = True
                await asyncio.sleep(1.0)
                continue
            if read_failed:
                log.info("voice task status read recovered task=%s turn=%s", task_id, turn_id)
            read_failed = False
            if view.version != previous_version:
                frame = task_frame(view, robot_id=robot_id, **correlation)
                if "request_key" in receipt:
                    frame["request_key"] = receipt["request_key"]
                yield frame
                previous_version = view.version
            if turn_finished(view):
                return
            await asyncio.sleep(0.25)

    def watch_task(self, session, receipt):
        """Start only after the task receipt has been sent on this connection."""
        turn_id = receipt["turn_id"]
        if turn_id in session["task_notifications"]:
            return
        notification = asyncio.create_task(self._notify_task(session, dict(receipt)))
        session["task_notifications"][turn_id] = notification
        self.notifications.add(notification)
        def finished(task):
            self.notifications.discard(task)
            if session["task_notifications"].get(turn_id) is task:
                del session["task_notifications"][turn_id]
        notification.add_done_callback(finished)

    async def _notify_task(self, session, receipt):
        binding = session["binding"]
        key = (binding["customer_id"], binding["binding_id"])
        context = TrustedContext(key[0], "robot", key[1])
        try:
            async for frame in self.task_notifications(context, receipt, binding["wire_robot_id"]):
                if self.sessions.get(key) is not session:
                    return
                await session["send"](frame)
                log.info("voice task notification type=%s task=%s turn=%s state=%s version=%s",
                         frame["type"], receipt["task_id"], receipt["turn_id"],
                         frame.get("state"), frame.get("version"))
        except (OSError, RuntimeError):
            log.warning("voice task notification connection lost task=%s turn=%s",
                        receipt["task_id"], receipt["turn_id"])
        except Exception as exc:
            log.error("voice task notification failed task=%s turn=%s error_type=%s",
                      receipt["task_id"], receipt["turn_id"], type(exc).__name__)

    async def close(self):
        pending = tuple(self.notifications)
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
