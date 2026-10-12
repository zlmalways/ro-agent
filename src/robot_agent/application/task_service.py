"""One task-write boundary: external effects run only after stored dispatch facts."""

from dataclasses import replace
from hashlib import sha256
from uuid import uuid4

from robot_agent.domain.models import (
    CandidatePlan, CandidateStep, ConflictError, NotFoundError, Plan, PlanError,
    Task, TaskView, TrustedContext,
)

from .executor import Executor
from .preflight import Preflight


def task_view(task):
    return TaskView(task.task_id, task.version, task.state, task.reason_code,
                    task.plan.plan_id if task.plan else None, task.clarification, task.result_disputed,
                    tuple({"command_id": command.command_id, "step_id": command.step_id,
                           "robot_binding_id": command.robot_binding_id, "action": command.wire_skill,
                           "args": dict(command.wire_args), "dispatch_state": command.dispatch_state,
                           "outcome": command.outcome, "evidence_ids": command.evidence_ids}
                          for command in task.commands))


class TaskService:
    def __init__(self, *, store, planner, robot, resources, policy, catalog):
        self.store = store
        self.planner = planner
        self.robot = robot
        self.resources = resources
        self.policy = policy
        self.catalog = catalog
        self.preflight = Preflight(catalog, policy)
        self.executor = Executor()

    async def submit(self, context, value, request_key):
        if not value.text.strip() or not request_key:
            raise PlanError("empty task/request key")
        digest = sha256(value.text.encode()).hexdigest()
        async with self.store.unit_of_work(context) as tx:
            existing = await tx.get_request(context.customer_id, context.actor_id, request_key)
            if existing:
                if existing[0] != digest:
                    raise ConflictError("request key content conflict")
                return task_view(await tx.load_task(existing[1]))
            task = Task(str(uuid4()), context.customer_id, context.actor_kind, context.actor_id, value.text,
                        state="PLANNING", unit_id=str(uuid4()))
            await tx.insert_task(task)
            await tx.put_request(context.customer_id, context.actor_id, request_key, digest, task.task_id)
        snapshot = await self.resources.snapshot(context)
        try:
            candidate = await self.planner.plan(value.text, [], snapshot)
            if candidate.outcome == "clarify":
                task = replace(task, state="NEEDS_CLARIFICATION", clarification=candidate.question)
            elif candidate.outcome == "unsupported":
                task = replace(task, state="FAILED", reason_code="UNSUPPORTED")
            else:
                check = await self.preflight.check(context, candidate, snapshot)
                state = {"allow": "READY", "deny": "FAILED", "require_confirmation": "WAITING_APPROVAL",
                         "unavailable": "BLOCKED"}[check.authorization]
                task = replace(task, state=state, reason_code="" if state == "READY" else "AUTHORIZATION_" + check.authorization.upper(),
                               snapshot=snapshot, plan=Plan(str(uuid4()), 1, check.validation.steps, check.validation.snapshot_id))
        except PlanError:
            task = replace(task, state="FAILED", reason_code="PLAN_INVALID")
        async with self.store.unit_of_work(context) as tx:
            await tx.save_task(task)
        return task_view(task)

    async def dispatch_next(self, deployment_id):
        async with self.store.unit_of_work(None) as tx:
            tasks = await tx.list_tasks()
        for task in tasks:
            if task.state not in {"READY", "RUNNING"}:
                continue
            context = TrustedContext(task.customer_id, task.actor_kind, task.actor_id)
            snapshot = await self.resources.snapshot(context)
            candidate = CandidatePlan("plan", tuple(CandidateStep(step.step_id, step.action, dict(step.args), step.depends_on)
                                                     for step in task.plan.steps))
            check = await self.preflight.check(context, candidate, snapshot, "dispatch")
            if check.authorization != "allow":
                continue
            async with self.store.unit_of_work(context) as tx:
                task = await tx.load_task(task.task_id)
                change = self.executor.prepare(context, None, task)
                if change.command is None or not await tx.claim_slot(task.task_id):
                    continue
                await tx.save_task(change.task)
            command = replace(change.command, dispatch_state="DISPATCHING")
            async with self.store.unit_of_work(context) as tx:
                task = await tx.load_task(task.task_id)
                task = replace(task, commands=tuple(command if item.command_id == command.command_id else item for item in task.commands))
                await tx.save_task(task)
            observation = await self.robot.send(command)
            async with self.store.unit_of_work(context) as tx:
                task = await tx.load_task(task.task_id)
                commands = tuple(replace(item, dispatch_state="SENT") if item.command_id == command.command_id and observation.sent else item
                                 for item in task.commands)
                await tx.save_task(replace(task, commands=commands))
            return

    async def on_robot_event(self, event):
        async with self.store.unit_of_work(None) as tx:
            for task in await tx.list_tasks():
                if task.customer_id != event.customer_id or not any(item.command_id == event.cmd_id for item in task.commands):
                    continue
                change = self.executor.apply_event(task, event)
                await tx.save_task(change.task)
                if change.task.state in {"SUCCEEDED", "FAILED", "CANCELLED"}:
                    await tx.release_slot(task.task_id)
                return

    async def get(self, context, task_id):
        async with self.store.unit_of_work(context) as tx:
            task = await tx.load_task(task_id)
            if task.customer_id != context.customer_id or task.actor_id != context.actor_id:
                raise NotFoundError(task_id)
            return task_view(task)
