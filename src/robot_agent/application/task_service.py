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
from robot_agent.domain.transitions import uncertain_execution


def task_view(task):
    return TaskView(task.task_id, task.version, task.state, task.reason_code,
                    task.plan.plan_id if task.plan else None, task.clarification, task.result_disputed,
                    tuple({"command_id": command.command_id, "step_id": command.step_id,
                           "robot_binding_id": command.robot_binding_id, "action": command.wire_skill,
                           "args": dict(command.wire_args), "dispatch_state": command.dispatch_state,
                           "outcome": command.outcome, "evidence_ids": command.evidence_ids}
                          for command in task.commands))


class TaskService:
    def __init__(self, *, store, planner, robot, resources, policy, catalog, queue_policy=None, ownership=None):
        self.store = store
        self.planner = planner
        self.robot = robot
        self.resources = resources
        self.policy = policy
        self.catalog = catalog
        self.preflight = Preflight(catalog, policy)
        self.executor = Executor()
        self.queue_policy = queue_policy
        self.ownership = ownership

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
        if self.queue_policy is None or self.ownership is None or not await self.ownership.healthy():
            return
        async with self.store.system_unit_of_work() as tx:
            tasks = await tx.list_tasks()
        for task in self.queue_policy.order(tasks):
            resuming_prepared = task.state == "BLOCKED" and task.reason_code == "REVALIDATION_REQUIRED"
            if task.state not in {"READY", "QUEUED", "RUNNING"} and not resuming_prepared:
                continue
            context = TrustedContext(task.customer_id, task.actor_kind, task.actor_id)
            snapshot = await self.resources.snapshot(context)
            candidate = CandidatePlan("plan", tuple(CandidateStep(step.step_id, step.action, dict(step.args), step.depends_on)
                                                     for step in task.plan.steps))
            check = await self.preflight.check(context, candidate, snapshot, "dispatch")
            if check.authorization != "allow":
                async with self.store.system_unit_of_work() as tx:
                    current = await tx.load_task(task.task_id, for_update=True)
                    if current.version != task.version:
                        continue
                    started = any(item.dispatch_state != "PREPARED" for item in current.commands)
                    state = "WAITING_APPROVAL" if check.authorization == "require_confirmation" and not started else "BLOCKED"
                    reason = "AUTHORIZATION_REQUIRED" if check.authorization == "require_confirmation" else "AUTHORIZATION_" + check.authorization.upper()
                    await tx.apply_changes(ChangeSet(replace(current, state=state, reason_code=reason,
                                                            version=current.version + 1), decisions=check.records))
                    await tx.append_event(current.task_id, TaskEvent("dispatch_blocked", {"reason_code": reason}))
                continue
            async with self.store.system_unit_of_work() as tx:
                task = await tx.load_task(task.task_id, for_update=True)
                binding = await tx.get_binding(task.customer_id, task.snapshot.robot_binding_id)
                if binding is None or not binding["enabled"] or binding["isolation_reason"]:
                    continue
                prepared = next((item for item in task.commands if item.dispatch_state == "PREPARED"), None)
                if resuming_prepared and prepared:
                    change = ChangeSet(replace(task, state="RUNNING", reason_code="", version=task.version + 1), prepared)
                else:
                    change = self.executor.prepare(context, None, task)
                if change.command:
                    if not prepared:
                        command = replace(change.command, dispatch_generation=binding["connection_generation"],
                                          owner_run_id=self.ownership.run_id)
                        change = replace(change, command=command,
                                         task=replace(change.task, commands=change.task.commands[:-1] + (command,)))
                if change.command is None or not await tx.claim_slot(deployment_id, change.task):
                    continue
                await tx.apply_changes(replace(change, decisions=check.records))
                await tx.append_event(task.task_id, TaskEvent("command_prepared", {"command_id": change.command.command_id}))
            # Renew checks after PREPARED is committed; external dependencies never wait inside the DB transaction.
            snapshot = await self.resources.snapshot(context)
            check = await self.preflight.check(context, candidate, snapshot, "dispatch")
            async with self.store.system_unit_of_work() as tx:
                task = await tx.load_task(task.task_id, for_update=True)
                binding = await tx.get_binding(task.customer_id, task.snapshot.robot_binding_id)
                if check.authorization != "allow" or not binding or not binding["enabled"] or binding["isolation_reason"]:
                    reason = "AUTHORIZATION_" + check.authorization.upper() if check.authorization != "allow" else "BINDING_ISOLATED"
                    await tx.apply_changes(ChangeSet(replace(task, state="BLOCKED", reason_code=reason,
                                                            version=task.version + 1), decisions=check.records))
                    await tx.append_event(task.task_id, TaskEvent("dispatch_blocked", {"reason_code": reason}))
                    return
                if task.state != "RUNNING" or task.result_disputed or not await self.ownership.healthy():
                    return
                stored = next(item for item in task.commands if item.command_id == change.command.command_id)
                if stored.dispatch_state != "PREPARED":
                    return
                command = replace(stored, dispatch_state="DISPATCHING", dispatch_generation=binding["connection_generation"],
                                  owner_run_id=self.ownership.run_id)
                task = replace(task, commands=tuple(command if item.command_id == command.command_id else item for item in task.commands),
                               version=task.version + 1)
                await tx.apply_changes(ChangeSet(task, command, decisions=check.records))
                await tx.append_event(task.task_id, TaskEvent("dispatching", {"command_id": command.command_id}))
            try:
                observation = await self.robot.send(command) if await self.ownership.healthy() else None
            except (OSError, RuntimeError):
                observation = None
            async with self.store.system_unit_of_work() as tx:
                task = await tx.load_task(task.task_id, for_update=True)
                commands = tuple(replace(item, dispatch_state="SENT") if item.command_id == command.command_id and observation and observation.sent else item
                                 for item in task.commands)
                if commands != task.commands:
                    await tx.apply_changes(ChangeSet(replace(task, commands=commands, version=task.version + 1)))
                    await tx.append_event(task.task_id, TaskEvent("send_observed", {"command_id": command.command_id}))
                elif not observation or not observation.sent:
                    change = uncertain_execution(task)
                    if change.task != task:
                        await tx.apply_changes(change)
                        await tx.append_event(task.task_id, TaskEvent("result_unknown", {"command_id": command.command_id}))
            return

    async def on_robot_event(self, event):
        async with self.store.system_unit_of_work() as tx:
            binding = await tx.get_binding(event.customer_id, event.robot_binding_id)
            if binding is None:
                return
            if event.connection_generation != binding["connection_generation"]:
                await tx.record_diagnostic(binding, event.connection_generation, event.event_id,
                                           {"cmd_id": event.cmd_id, "state": event.state, "reason": "STALE_CONNECTION"})
                return
            related = await tx.tasks_for_binding(binding)
            task = next((task for task in related if any(command.command_id == event.cmd_id for command in task.commands)), None)
            if task is None:
                await tx.record_diagnostic(binding, event.connection_generation, event.event_id,
                                           {"cmd_id": event.cmd_id, "state": event.state, "reason": "UNKNOWN_COMMAND"})
                return
            if await tx.event_seen(event):
                return
            change = self.executor.apply_event(task, event, current_generation=binding["connection_generation"])
            if change.task == task:
                return
            await tx.apply_changes(change)
            await tx.append_event(task.task_id, TaskEvent("robot_feedback", {"command_id": event.cmd_id,
                                                                          "state": change.task.state}))
            if change.command and change.command.evidence_conflict:
                await tx.isolate_binding(binding, "EVIDENCE_CONFLICT", event.event_id)
                for other in related:
                    if other.task_id != task.task_id and other.state not in {"SUCCEEDED", "FAILED", "CANCELLED"}:
                        blocked = replace(other, state="BLOCKED", reason_code="EVIDENCE_CONFLICT",
                                          result_disputed=True, version=other.version + 1)
                        await tx.apply_changes(ChangeSet(blocked))
                        await tx.append_event(other.task_id, TaskEvent("binding_isolated", {"reason_code": "EVIDENCE_CONFLICT"}))
            if change.task.state in {"SUCCEEDED", "FAILED", "CANCELLED"}:
                await tx.release_slot(task.task_id)

    async def on_robot_connected(self, binding, frame):
        async with self.store.system_unit_of_work() as tx:
            stored = await tx.get_binding(binding["customer_id"], binding["binding_id"])
            if stored is None or not stored["enabled"] or stored["wire_robot_id"] != frame["robot_id"]:
                raise PermissionError("robot binding unavailable")
            tasks = await tx.tasks_for_binding(binding)
            generation = await tx.activate_binding(stored, frame)
            await self._observe_busy(tx, stored, generation, frame, tasks)
            return generation

    async def _observe_busy(self, tx, binding, generation, frame, tasks):
        command_id = frame.get("busy_cmd_id", "")
        tracked = any(command.command_id == command_id and command.dispatch_state != "PREPARED"
                      and command.outcome not in {"SUCCEEDED", "FAILED"} for task in tasks for command in task.commands)
        has_active = any(command.dispatch_state != "PREPARED" and command.outcome not in {"SUCCEEDED", "FAILED"}
                         for task in tasks for command in task.commands)
        if (command_id and not tracked) or (frame.get("busy") is True and not has_active):
            evidence_id = str(uuid4())
            await tx.record_diagnostic(binding, generation, evidence_id, {"reason": "UNTRACKED_EXECUTION", "busy_cmd_id": command_id})
            await tx.isolate_binding(binding, "UNTRACKED_EXECUTION", evidence_id)
            for task in tasks:
                if task.state not in {"SUCCEEDED", "FAILED", "CANCELLED"}:
                    changed = replace(task, state="BLOCKED", reason_code="UNTRACKED_EXECUTION", version=task.version + 1)
                    await tx.apply_changes(ChangeSet(changed))
                    await tx.append_event(task.task_id, TaskEvent("binding_isolated", {"reason_code": "UNTRACKED_EXECUTION"}))

    async def on_robot_observation(self, binding, generation, frame):
        async with self.store.system_unit_of_work() as tx:
            stored = await tx.get_binding(binding["customer_id"], binding["binding_id"])
            if stored is None or stored["connection_generation"] != generation:
                return
            tasks = await tx.tasks_for_binding(binding)
            await tx.update_observations(stored, generation, frame)
            await self._observe_busy(tx, stored, generation, frame, tasks)

    async def on_robot_disconnected(self, binding, generation):
        async with self.store.system_unit_of_work() as tx:
            stored = await tx.get_binding(binding["customer_id"], binding["binding_id"])
            if stored is None or stored["connection_generation"] != generation:
                return
            for task in await tx.tasks_for_binding(binding):
                change = uncertain_execution(task)
                if change.task != task:
                    await tx.apply_changes(change)
                    await tx.append_event(task.task_id, TaskEvent("result_unknown", {"reason_code": "RESULT_UNKNOWN"}))
            await tx.update_observations(binding, generation, {"connected": False})

    async def recover(self, deployment_id):
        if self.ownership is None or not await self.ownership.healthy():
            return
        async with self.store.system_unit_of_work() as tx:
            tasks = await tx.list_tasks()
            for original in sorted(tasks, key=lambda value: value.task_id):
                task = await tx.load_task(original.task_id, for_update=True)
                if task.state in {"SUCCEEDED", "FAILED", "CANCELLED"}:
                    continue
                change = self.executor.reconcile(task)
                if change.task != task:
                    await tx.apply_changes(change)
                    await tx.append_event(task.task_id, TaskEvent("reconciled", {"reason_code": change.task.reason_code}))

    async def may_write(self, command):
        if self.ownership is None or not await self.ownership.healthy() or command.owner_run_id != self.ownership.run_id:
            return False
        async with self.store.system_unit_of_work() as tx:
            try:
                task = await tx.load_task(command.task_id, for_update=True)
            except NotFoundError:
                return False
            if task.customer_id != command.customer_id:
                return False
            binding = await tx.get_binding(command.customer_id, command.robot_binding_id)
            stored = next((item for item in task.commands if item.command_id == command.command_id), None)
            return (binding is not None and binding["enabled"] and not binding["isolation_reason"]
                    and binding["connection_generation"] == command.dispatch_generation
                    and stored is not None and stored.dispatch_state == "DISPATCHING"
                    and stored.outcome not in {"SUCCEEDED", "FAILED"} and task.state == "RUNNING")

    async def get(self, context, task_id):
        async with self.store.unit_of_work(context) as tx:
            task = await tx.load_task(task_id)
            if task.customer_id != context.customer_id or task.actor_id != context.actor_id:
                raise NotFoundError(task_id)
            return task_view(task)
