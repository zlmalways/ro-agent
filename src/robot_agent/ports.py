"""External boundaries; test substitutes remain under tests/ only."""

from contextlib import AbstractAsyncContextManager
from typing import Protocol

from robot_agent.domain.models import CandidatePlan, Command, ContextSnapshot, SendObservation, TrustedContext


class PlannerPort(Protocol):
    async def plan(self, goal: str, answers: list[str], snapshot: ContextSnapshot) -> CandidatePlan: ...


class RobotPort(Protocol):
    async def send(self, command: Command) -> SendObservation: ...


class Store(Protocol):
    def unit_of_work(self, context: TrustedContext | None) -> AbstractAsyncContextManager: ...
