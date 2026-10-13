"""Normalize provisioned robot voice input; task decisions stay in TaskService."""

import asyncio
from uuid import uuid4

from robot_agent.domain.models import DependencyUnavailable, NotFoundError, PlanError, TrustedContext


class VoiceService:
    def __init__(self, *, inputs, output, allow_woken_audio, max_audio_bytes, duplicate_policy=None):
        self.inputs = inputs
        self.output = output
        self.allow_woken_audio = allow_woken_audio
        self.max_audio_bytes = max_audio_bytes
        self.duplicate_policy = duplicate_policy
        self.sessions = {}
        self.notifications = set()

    async def submit(self, context, robot_id, audio, request_key, *, woke):
        error, view = None, None
        if not self.allow_woken_audio or woke is not True:
            error = "VOICE_NOT_AWAKE"
        elif not request_key and self.duplicate_policy != "per_turn":
            error = "VOICE_DUPLICATE_POLICY_UNAVAILABLE"
        else:
            try:
                view = await self.inputs.audio(context, audio, "audio/wav", request_key or str(uuid4()))
            except (DependencyUnavailable, PlanError) as exc:
                error = str(exc)
        frames = await self.output.render(robot_id=robot_id, transcript="", view=view, error=error)
        for frame in frames:
            if frame["type"] == "done":
                frame.update(member_id=None, guest_id=None, speaker_candidate=None)
        return frames

    def register(self, binding, send):
        key = (binding["customer_id"], binding["binding_id"])
        session = {"binding": binding, "send": send, "lock": asyncio.Lock(), "pcm": bytearray()}
        self.sessions[key] = session
        return session

    def unregister(self, session):
        key = (session["binding"]["customer_id"], session["binding"]["binding_id"])
        if self.sessions.get(key) is session:
            del self.sessions[key]

    async def deliver(self, session, audio, *, woke):
        binding = session["binding"]
        if self.sessions.get((binding["customer_id"], binding["binding_id"])) is not session:
            return
        context = TrustedContext(binding["customer_id"], "robot", binding["binding_id"])
        async with session["lock"]:
            await session["send"]({"type": "state", "state": "thinking"})
            frames = await self.submit(context, binding["wire_robot_id"], audio, "", woke=woke)
            await self._send_tail(session, frames)

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
        if session is None or view.state not in {"SUCCEEDED", "FAILED", "CANCELLED", "BLOCKED"}:
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
