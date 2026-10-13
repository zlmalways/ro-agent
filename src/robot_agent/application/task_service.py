"""One task-write boundary: external effects run only after stored dispatch facts."""

from dataclasses import replace
from hashlib import sha256
import json
from uuid import uuid4

from robot_agent.domain.models import (
    CandidatePlan, CandidateStep, ChangeSet, ConflictError, NotFoundError, Plan, PlanError,
    Task, TaskEvent, TaskView, TrustedContext,
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
        digest = sha256(json.dumps({"channel": value.channel, "text": value.text, "source_ref": value.source_ref},
                                  sort_keys=True, separators=(",", ":")).encode()).hexdigest()
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
            await tx.append_event(task.task_id, TaskEvent("received", {"state": task.state}))
        snapshot = await self.resources.snapshot(context)
        records = ()
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
                records = check.records
        except PlanError:
            task = replace(task, state="FAILED", reason_code="PLAN_INVALID")
        async with self.store.unit_of_work(context) as tx:
            current = await tx.load_task(task.task_id, for_update=True)
            if current.version != task.version:
                raise ConflictError("planning task changed")
            task = replace(task, version=task.version + 1)
            await tx.apply_changes(ChangeSet(task, decisions=records))
            await tx.append_event(task.task_id, TaskEvent("planned", {"state": task.state, "reason_code": task.reason_code}))
        return task_view(task)

    async def dispatch_next(self, deployment_id):
        async with self.store.system_unit_of_work() as tx:
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
            async with self.store.system_unit_of_work() as tx:
                task = await tx.load_task(task.task_id, for_update=True)
                change = self.executor.prepare(context, None, task)
                if change.command is None or not await tx.claim_slot(deployment_id, change.task):
                    continue
                await tx.apply_changes(replace(change, decisions=check.records))
                await tx.append_event(task.task_id, TaskEvent("command_prepared", {"command_id": change.command.command_id}))
            command = replace(change.command, dispatch_state="DISPATCHING")
            async with self.store.system_unit_of_work() as tx:
                task = await tx.load_task(task.task_id, for_update=True)
                task = replace(task, commands=tuple(command if item.command_id == command.command_id else item for item in task.commands),
                               version=task.version + 1)
                await tx.apply_changes(ChangeSet(task, command))
                await tx.append_event(task.task_id, TaskEvent("dispatching", {"command_id": command.command_id}))
            observation = await self.robot.send(command)
            async with self.store.system_unit_of_work() as tx:
                task = await tx.load_task(task.task_id, for_update=True)
                commands = tuple(replace(item, dispatch_state="SENT") if item.command_id == command.command_id and observation.sent else item
                                 for item in task.commands)
                if commands != task.commands:
                    await tx.apply_changes(ChangeSet(replace(task, commands=commands, version=task.version + 1)))
                    await tx.append_event(task.task_id, TaskEvent("send_observed", {"command_id": command.command_id}))
            return

    async def on_robot_event(self, event):
        async with self.store.system_unit_of_work() as tx:
            task = await tx.find_event_task(event)
            if task is None or await tx.event_seen(event):
                return
            change = self.executor.apply_event(task, event)
            if change.task == task:
                return
            await tx.apply_changes(change)
            await tx.append_event(task.task_id, TaskEvent("robot_feedback", {"command_id": event.cmd_id,
                                                                          "state": change.task.state}))
            if change.task.state in {"SUCCEEDED", "FAILED", "CANCELLED"}:
                await tx.release_slot(task.task_id)

    async def get(self, context, task_id):
        async with self.store.unit_of_work(context) as tx:
            task = await tx.load_task(task_id)
            if task.customer_id != context.customer_id or task.actor_id != context.actor_id:
                raise NotFoundError(task_id)
            return task_view(task)
