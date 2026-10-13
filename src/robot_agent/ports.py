"""External boundaries; test substitutes remain under tests/ only."""

from contextlib import AbstractAsyncContextManager
from typing import Protocol

from robot_agent.domain.models import (
    CandidatePlan, ChangeSet, Command, ContextSnapshot, SendObservation, Task, TaskEvent, TrustedContext,
)


class PlannerPort(Protocol):
    async def plan(self, goal: str, answers: list[str], snapshot: ContextSnapshot) -> CandidatePlan: ...


class RobotPort(Protocol):
    async def send(self, command: Command) -> SendObservation: ...


class TaskTransaction(Protocol):
    async def load_facts(self, task_id: str, *, for_update: bool) -> Task: ...

    async def apply_changes(self, changes: ChangeSet) -> None: ...

    async def append_event(self, task_id: str, value: TaskEvent) -> None: ...


class Store(Protocol):
    def unit_of_work(self, context: TrustedContext) -> AbstractAsyncContextManager[TaskTransaction]: ...

    def system_unit_of_work(self) -> AbstractAsyncContextManager[TaskTransaction]: ...
