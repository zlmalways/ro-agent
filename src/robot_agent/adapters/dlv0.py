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
        self.write_locks = {}
        self.on_result = None

    def _write_lock(self, customer_id, binding_id):
        # Registration/replacement, writes and unregister share one lock per robot.
        # Retain it while the adapter lives so an old waiter cannot use another lock.
        return self.write_locks.setdefault((customer_id, binding_id), asyncio.Lock())

    def connected_robots(self, customer_id):
        """Copy public connection coordinates, never credentials or send handles."""
        return {binding_id: session.generation
                for (customer, binding_id), session in self.sessions.items() if customer == customer_id}

    def _bounded_frame(self, frame):
        if len(json.dumps(frame).encode()) > self.max_frame_bytes:
            raise PlanError("robot frame size exceeded")
        return parse_frame(frame)

    async def register(self, binding, frame, send):
        frame = self._bounded_frame(frame)
        if frame["type"] != "hello" or frame["robot_id"] != binding["wire_robot_id"]:
            raise PermissionError("robot_id mismatch")
        async with self._write_lock(binding["customer_id"], binding["binding_id"]):
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
        if self.write_gate is None or not await self.write_gate.healthy():
            return SendObservation(False, False, "OWNERSHIP_UNAVAILABLE", "runtime")
        async with self._write_lock(command.customer_id, command.robot_binding_id):
            if not await self.write_gate.healthy():
                return SendObservation(False, False, "OWNERSHIP_UNAVAILABLE", "runtime")
            session = self.sessions.get((command.customer_id, command.robot_binding_id))
            if session is None or session.generation != command.dispatch_generation:
                return SendObservation(False, False, "ROBOT_DISCONNECTED")
            permission = await self.service.may_write(command)
            if not permission:
                return permission if isinstance(permission, SendObservation) else SendObservation(False, False, "SEND_NOT_ATTEMPTED")
            frame = command_frame(command)
            try:
                await session.send(frame)
            except (OSError, RuntimeError):
                return SendObservation(False, True, "SEND_FAILED")
            return SendObservation(True)

    async def unregister(self, session):
        async with self._write_lock(session.binding["customer_id"], session.binding["binding_id"]):
            key = (session.binding["customer_id"], session.binding["binding_id"])
            if self.sessions.get(key) is session:
                del self.sessions[key]
                await self.service.on_robot_disconnected(session.binding, session.generation)

    async def cancel(self, command):
        if self.write_gate is None or not await self.write_gate.healthy():
            return SendObservation(False, False, "OWNERSHIP_UNAVAILABLE", "runtime")
        async with self._write_lock(command.customer_id, command.robot_binding_id):
            if not await self.write_gate.healthy():
                return SendObservation(False, False, "OWNERSHIP_UNAVAILABLE", "runtime")
            session = self.sessions.get((command.customer_id, command.robot_binding_id))
            if session is None:
                return SendObservation(False, False, "ROBOT_DISCONNECTED")
            try:
                await session.send(cancel_frame(command.command_id))
            except (OSError, RuntimeError):
                return SendObservation(False, True, "SEND_FAILED")
            return SendObservation(True)
