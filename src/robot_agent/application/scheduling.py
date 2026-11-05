"""Allocate the sole available robot; execution checks still belong to Preflight."""

from dataclasses import dataclass
from typing import Sequence

from robot_agent.domain.models import ContextSnapshot, RobotAllocation, Task, TaskInput


@dataclass(frozen=True)
class SchedulingResult:
    snapshot: ContextSnapshot | None = None
    reason_code: str = ""


def select_single_candidate(task: TaskInput, candidates: Sequence[ContextSnapshot]) -> SchedulingResult:
    # The task is the scheduling input boundary. This first policy only counts candidates.
    if not candidates:
        return SchedulingResult(reason_code="NO_ONLINE_ROBOT")
    if len(candidates) != 1:
        return SchedulingResult(reason_code="MULTI_ROBOT_SCHEDULING_NOT_IMPLEMENTED")
    return SchedulingResult(snapshot=candidates[0])


@dataclass(frozen=True)
class AllocationResult:
    status: str
    allocation: RobotAllocation | None = None
    reason_code: str = ""


def available_choice(task, candidates, allocations):
    """One policy for both candidate selection and competition with older requests."""
    own = next((item for item in allocations.values() if item.task_id == task.task_id
                and item.customer_id == task.customer_id), None)
    target = task.snapshot.robot_binding_id if task.plan else own.robot_binding_id if own else None
    considered = [s for s in candidates if not target or s.robot_binding_id == target]
    if not considered:
        return None, "NO_ONLINE_ROBOT", False
    available, unknown, occupied = [], False, False
    for snapshot in considered:
        holder = allocations.get(snapshot.robot_binding_id)
        if holder and holder != own:
            occupied = True
            continue
        if own and own.snapshot.generation != snapshot.generation:
            return None, "CONNECTION_CHANGED", False
        busy = snapshot.readiness.get("busy")
        if busy is False:
            available.append(snapshot)
        elif busy is not True:
            unknown = True
    if available:
        choice = select_single_candidate(TaskInput(task.task_id, "task", task.goal_text, ""), available)
        return choice.snapshot, choice.reason_code, False
    if unknown:
        return None, "ROBOT_STATE_UNKNOWN", False
    return None, "EXECUTION_CAPACITY_BUSY" if occupied else "ROBOT_BUSY", True


async def schedule(task: Task, candidates: Sequence[ContextSnapshot], tx, deployment_id: str) -> AllocationResult:
    """Caller holds a short transaction lock through choosing and recording."""
    if any(snapshot.customer_id != task.customer_id for snapshot in candidates):
        return AllocationResult("blocked", reason_code="CANDIDATE_CUSTOMER_MISMATCH")
    allocations = await tx.allocations(task.customer_id)
    own = await tx.task_allocation(task)
    snapshot, reason, waiting = available_choice(task, candidates, allocations)
    if snapshot is None:
        return AllocationResult("waiting" if waiting else "blocked", own, reason)
    if own is None:
        for prior in await tx.prior_waiters(task):
            if prior.cancel_requested or prior.result_disputed:
                continue
            choice, _, _ = available_choice(prior, candidates, allocations)
            if choice and choice.robot_binding_id == snapshot.robot_binding_id:
                return AllocationResult("waiting", reason_code="QUEUE_ORDER")
        own = await tx.claim_slot(deployment_id, task, snapshot)
        if not isinstance(own, RobotAllocation):
            raise RuntimeError("allocation store did not return its committed record")
    return AllocationResult("assigned", own)
