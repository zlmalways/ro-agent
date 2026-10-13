"""Authenticated dlv/0 transport."""

import asyncio
from dataclasses import dataclass
import json
from uuid import uuid4

from robot_agent.domain.models import PlanError, RobotEvent, SendObservation
from robot_agent.protocol.dlv0_schema import cancel_frame, command_frame, parse_frame


@dataclass(frozen=True)
class RobotSession:
    binding: dict
    generation: int
    send: object


class Dlv0Robot:
    def __init__(self, *, service, write_gate, max_frame_bytes):
        self.service = service
        self.write_gate = write_gate
        self.max_frame_bytes = max_frame_bytes
        self.sessions = {}
        self.write_lock = asyncio.Lock()
        self.on_result = None

    def _bounded_frame(self, frame):
        if len(json.dumps(frame).encode()) > self.max_frame_bytes:
            raise PlanError("robot frame size exceeded")
        return parse_frame(frame)

    async def register(self, binding, frame, send):
        frame = self._bounded_frame(frame)
        if frame["type"] != "hello" or frame["robot_id"] != binding["wire_robot_id"]:
            raise PermissionError("robot_id mismatch")
        async with self.write_lock:
            generation = await self.service.on_robot_connected(binding, frame)
            session = RobotSession(dict(binding), generation, send)
            self.sessions[binding["customer_id"], binding["binding_id"]] = session
            return session

    async def handle(self, session, frame):
        key = (session.binding["customer_id"], session.binding["binding_id"])
        if self.sessions.get(key) is not session:
            return
        frame = self._bounded_frame(frame)
        if frame["type"] == "cmd_status":
            event = RobotEvent(str(uuid4()), key[0], key[1], session.generation, frame["cmd_id"], frame["state"],
                               details={field: frame[field] for field in ("detail", "error_code") if field in frame})
            view = await self.service.on_robot_event(event)
            if view and self.on_result:
                await self.on_result(key[0], key[1], view)
        elif frame["type"] in {"readiness", "telemetry"}:
            await self.service.on_robot_observation(session.binding, session.generation, frame)
        elif frame["type"] == "hello":
            # A repeated hello cannot silently create a fresh generation or replace another robot.
            if frame["robot_id"] != session.binding["wire_robot_id"]:
                raise PermissionError("robot_id mismatch")
            await self.service.on_robot_observation(session.binding, session.generation, frame)

    async def send(self, command):
        async with self.write_lock:
            if not await self.write_gate.healthy():
                return SendObservation(False)
            session = self.sessions.get((command.customer_id, command.robot_binding_id))
            if session is None or session.generation != command.dispatch_generation:
                return SendObservation(False)
            if not await self.service.may_write(command):
                return SendObservation(False)
            try:
                await session.send(command_frame(command))
            except (OSError, RuntimeError):
                return SendObservation(False)
            return SendObservation(True)

    async def unregister(self, session):
        async with self.write_lock:
            key = (session.binding["customer_id"], session.binding["binding_id"])
            if self.sessions.get(key) is session:
                del self.sessions[key]
                await self.service.on_robot_disconnected(session.binding, session.generation)

    async def cancel(self, command):
        async with self.write_lock:
            if not await self.write_gate.healthy():
                return SendObservation(False)
            session = self.sessions.get((command.customer_id, command.robot_binding_id))
            if session is None:
                return SendObservation(False)
            try:
                await session.send(cancel_frame(command.command_id))
            except (OSError, RuntimeError):
                return SendObservation(False)
            return SendObservation(True)
