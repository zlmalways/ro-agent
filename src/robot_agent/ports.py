"""External boundaries; test substitutes remain under tests/ only."""

from contextlib import AbstractAsyncContextManager
from typing import Mapping, Protocol

from robot_agent.domain.models import (
    CandidatePlan, ChangeSet, Command, ContextSnapshot, SendObservation, Task, TaskEvent, TrustedContext,
)


class PlannerPort(Protocol):
    async def plan(self, goal: str, answers: list[str], snapshot: ContextSnapshot) -> CandidatePlan: ...


class ConnectedRobots(Protocol):
    def __call__(self, customer_id: str) -> Mapping[str, int]: ...


class ResourcesPort(Protocol):
    async def snapshot(self, context: TrustedContext, binding_id: str | None = None) -> ContextSnapshot: ...

    async def online_robot_details(self, context: TrustedContext) -> list[ContextSnapshot]: ...


class RobotPort(Protocol):
    def connected_robots(self, customer_id: str) -> Mapping[str, int]: ...

    async def send(self, command: Command) -> SendObservation: ...


class TaskTransaction(Protocol):
    async def task_allocation_binding(self, task: Task) -> str | None: ...

    async def load_facts(self, task_id: str, *, for_update: bool) -> Task: ...

    async def apply_changes(self, changes: ChangeSet) -> None: ...

    async def append_event(self, task_id: str, value: TaskEvent) -> None: ...


class Store(Protocol):
    def unit_of_work(self, context: TrustedContext) -> AbstractAsyncContextManager[TaskTransaction]: ...

    def system_unit_of_work(self) -> AbstractAsyncContextManager[TaskTransaction]: ...
