"""One task-write boundary: external effects run only after stored dispatch facts."""

import asyncio
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
from uuid import uuid4

from robot_agent.domain.models import (
    ChangeSet, ConflictError, Decision, DependencyUnavailable, NotFoundError, Plan, PlanError, PreflightBlocked,
    RobotAllocation, Task, TaskEvent, TaskView, TrustedContext,
)

from .scheduling import schedule
from .executor import Executor
from .preflight import Preflight, approval_covers
from robot_agent.domain.transitions import inflight_commands, is_abandoned, uncertain_execution


def step_description(action, args, labels):
    if action == "move_to":
        return "导航到：" + str(args.get("location", ""))
    skill_id = str(args.get("skill_id", ""))
    return "调用技能：" + labels.get(skill_id, skill_id)


def task_view(task, allocation_robot_binding_id=None):
    return TaskView(task.task_id, task.version, task.state, task.reason_code,
                    task.plan.plan_id if task.plan else None, task.clarification, task.result_disputed,
                    tuple({"command_id": command.command_id, "step_id": command.step_id,
                           "robot_binding_id": command.robot_binding_id, "action": command.wire_skill,
                           "args": dict(command.wire_args), "dispatch_state": command.dispatch_state,
                           "outcome": command.outcome, "evidence_ids": command.evidence_ids,
                           "description": ("导航到：" + str(command.wire_args.get("location", ""))
                                           if command.wire_skill == "move_to" else "调用技能：" + str(command.wire_args.get("skill_id", "")))}
                          for command in task.commands), task.clarification_id,
                    tuple({"step_id": step.step_id, "action": step.action, "args": dict(step.args), "depends_on": step.depends_on,
                           "source_refs": step.source_refs,
                           "description": step_description(step.action, step.args, task.snapshot.capability_labels)}
                          for step in task.plan.steps) if task.plan else (), goal_text=task.goal_text,
                    allocation_robot_binding_id=allocation_robot_binding_id,
                    plan_robot_binding_id=task.snapshot.robot_binding_id if task.plan and task.snapshot else None)


@dataclass
class PlanningWork:
    task: Task
    allocation: RobotAllocation
    job: asyncio.Task | None = None
    completing: bool = False


class TaskService:
    def __init__(self, *, store, planner, robot, resources, policy, catalog, queue_policy=None, ownership=None,
                 max_text_chars=None, max_plan_steps=None, command_timeout_seconds=None):
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
        self.max_text_chars = max_text_chars
        self.max_plan_steps = max_plan_steps
        self.command_timeout_seconds = command_timeout_seconds
        self._planning = {}
        self._dispatches = {}
        self._work_errors = {}
        self._closing = False

    async def view_in_transaction(self, tx, task):
        return task_view(task, await tx.task_allocation_binding(task))

    async def submit(self, context, value, request_key, *, wait_for_plan=False):
        if not value.text.strip() or not request_key:
            raise PlanError("empty task/request key")
        if self.max_text_chars is not None and len(value.text) > self.max_text_chars:
            raise PlanError("TEXT_LIMIT_EXCEEDED")
        digest = sha256(json.dumps({"channel": value.channel, "text": value.text, "source_ref": value.source_ref},
                                  sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        async with self.store.unit_of_work(context) as tx:
            existing = await tx.get_request(context.customer_id, context.actor_id, request_key)
            if existing:
                if existing[0] != digest:
                    raise ConflictError("request key content conflict")
                task = await tx.load_task(existing[1], for_update=True)
                if task.state != "BLOCKED" or task.resume_phase != "PLANNING":
                    return await self.view_in_transaction(tx, task)
                task = replace(task, state="RECEIVED", reason_code="", version=task.version + 1)
                await tx.apply_changes(ChangeSet(task))
                await tx.append_event(task.task_id, TaskEvent("planning_retried", {"state": task.state}))
            else:
                task = Task(str(uuid4()), context.customer_id, context.actor_kind, context.actor_id, value.text,
                            state="RECEIVED", unit_id=str(uuid4()))
                await tx.insert_task(task)
                await tx.put_request(context.customer_id, context.actor_id, request_key, digest, task.task_id)
                await tx.append_event(task.task_id, TaskEvent("received", {"state": task.state}))
        if wait_for_plan:
            # Legacy robot voice replies include a planning question or approval
            # notice. This changes response timing only, not admission/scheduling.
            await self.schedule_pending(self.store.deployment_id, task_id=task.task_id)
            return await self.get(context, task.task_id)
        # Admission is committed before replying. The existing durable runtime scans
        # RECEIVED tasks; no model/network work belongs to this HTTP round trip.
        return task_view(task)

    def _watch_work(self, job, registry, key, entry):
        def finished(done):
            if registry.get(key) is entry:
                del registry[key]
            if not done.cancelled():
                error = done.exception()  # Always retrieve failures, including detached work.
                if error is not None:
                    self._work_errors[done] = error
        job.add_done_callback(finished)

    async def _join_work(self, jobs):
        jobs = list(dict.fromkeys(job for job in jobs if job is not None))
        if not jobs:
            return
        # An HTTP caller disappearing must not cancel admitted work.
        outcomes = await asyncio.gather(*(asyncio.shield(job) for job in jobs), return_exceptions=True)
        for job in jobs:
            self._work_errors.pop(job, None)
        for outcome in outcomes:
            if isinstance(outcome, Exception):
                raise outcome

    def raise_background_error(self):
        if self._work_errors:
            job = next(iter(self._work_errors))
            raise self._work_errors.pop(job)

    async def close_work(self):
        self._closing = True
        jobs = [work.job for work in self._planning.values()] + list(self._dispatches.values())
        for job in jobs:
            if not job.done():
                job.cancel()
        if jobs:
            await asyncio.gather(*jobs, return_exceptions=True)
        self._planning.clear()
        self._dispatches.clear()

    async def _cancel_invalid_planning(self):
        # Do not use the earlier list_tasks snapshot: a queued task may have
        # acquired its allocation since that scan (the historical M4 race).
        for work in list(self._planning.values()):
            async with self.store.system_unit_of_work() as tx:
                if work.job.done() or work.job.cancelling() or work.completing:
                    continue
                current = await tx.load_task(work.task.task_id, for_update=True)
                if await self._abandon_if_disabled(tx, current):
                    work.job.cancel()
                    continue
                allocation = await tx.task_allocation(current)
                binding = await tx.get_binding(current.customer_id, work.allocation.robot_binding_id)
                valid_binding = (binding and binding["enabled"] and not binding["isolation_reason"]
                                 and binding["connection_generation"] == work.allocation.snapshot.generation)
                valid = (current.state == "PLANNING" and not current.cancel_requested and not current.result_disputed
                         and current.version == work.task.version and allocation is not None
                         and allocation.allocation_id == work.allocation.allocation_id and valid_binding)
                if not valid:
                    if current.state == "PLANNING" and current.version == work.task.version:
                        changed = replace(current, state="BLOCKED", reason_code="REVALIDATION_REQUIRED",
                                          resume_phase="PLANNING", version=current.version + 1)
                        await tx.apply_changes(ChangeSet(changed))
                        await tx.append_event(current.task_id, TaskEvent("planning_interrupted", {"reason_code": changed.reason_code}))
                        await self._release_unstarted(tx, changed)
                    work.job.cancel()

    async def _run_planning(self, work):
        try:
            # Starting after commit also covers cancellation between commit and launch.
            async with self.store.system_unit_of_work() as tx:
                current = await tx.load_task(work.task.task_id, for_update=True)
                allocation = await tx.task_allocation(current)
                if (current.state != "PLANNING" or current.version != work.task.version or allocation is None
                        or allocation.allocation_id != work.allocation.allocation_id):
                    return
            await self._generate_plan(work.task, work.allocation.snapshot, work=work)
        except asyncio.CancelledError:
            # A shutdown/explicit cancellation invalidates unsent planning only.
            async with self.store.system_unit_of_work() as tx:
                current = await tx.load_task(work.task.task_id, for_update=True)
                if current.state == "PLANNING" and current.version == work.task.version:
                    reason = ("OWNERSHIP_UNAVAILABLE" if self.ownership is not None and not await self.ownership.healthy()
                              else "PLANNING_INTERRUPTED")
                    changed = replace(current, state="BLOCKED", reason_code=reason,
                                      resume_phase="PLANNING", version=current.version + 1)
                    await tx.apply_changes(ChangeSet(changed))
                    await tx.append_event(current.task_id, TaskEvent("planning_interrupted", {"reason_code": changed.reason_code}))
                    await self._release_unstarted(tx, changed)
            raise
        except (DependencyUnavailable, PreflightBlocked):
            raise  # The precise provider/check failure was already saved.

    async def schedule_pending(self, deployment_id, *, task_id=None, wait_for_plan=True):
        if self._closing or (self.ownership is not None and not await self.ownership.healthy()):
            return
        await self._abandon_disabled_tasks()
        await self._cancel_invalid_planning()
        if task_id is not None:
            identifiers = [task_id]
        else:
            if self.queue_policy is None:
                return
            async with self.store.system_unit_of_work() as tx:
                tasks = await tx.list_tasks()
            identifiers = [task.task_id for task in self.queue_policy.order(tasks)
                           if task.state in {"RECEIVED", "QUEUED", "READY"}]
        jobs = []
        for identifier in identifiers:
            if self._closing or (self.ownership is not None and not await self.ownership.healthy()):
                break
            jobs.append(await self._schedule_task(identifier, deployment_id))
        if wait_for_plan:
            await self._join_work(jobs)

    async def _schedule_task(self, task_id, deployment_id):
        async with self.store.system_unit_of_work() as tx:
            task = await tx.load_task(task_id)
            if task.state == "PLANNING":
                work = self._planning.get((task.customer_id, task.task_id))
                return work.job if work else None
            if task.state not in {"RECEIVED", "QUEUED", "READY"} or task.cancel_requested or task.result_disputed:
                return None
            occupied = await tx.task_allocation(task)
            if task.state == "READY" and occupied and occupied.task_id == task.task_id:
                return None
        context = TrustedContext(task.customer_id, task.actor_kind, task.actor_id)
        try:
            # FIFO checks need the same complete candidate set as new requests;
            # available_choice keeps an existing plan fixed to its original robot.
            candidates = await self.resources.online_robot_details(context)
        except DependencyUnavailable as exc:
            await self._block_current(task, str(exc), resume_phase="PLANNING" if not task.plan else "")
            raise
        async with self.store.system_unit_of_work() as tx:
            current = await tx.load_task(task_id, for_update=True)
            if await self._abandon_if_disabled(tx, current):
                return None
            if current.version != task.version:
                return None
            # Recheck DB evidence after the asynchronous resource read, inside the slot lock.
            verified = []
            for snapshot in candidates:
                binding = await tx.get_binding(task.customer_id, snapshot.robot_binding_id)
                if (binding and binding["enabled"] and not binding["isolation_reason"]
                        and (not snapshot.binding_version or (binding["version"] == snapshot.binding_version
                             and binding["connection_generation"] == snapshot.generation))):
                    verified.append(snapshot)
            result = await schedule(task, verified, tx, deployment_id)
            if result.status == "assigned":
                state, reason, phase = ("READY" if task.plan else "PLANNING"), "", ""
            else:
                waiting = result.status == "waiting" or (task.state == "QUEUED" and result.reason_code in {
                    "NO_ONLINE_ROBOT", "ROBOT_STATE_UNKNOWN", "CONNECTION_CHANGED"})
                state, reason, phase = ("QUEUED" if waiting else "BLOCKED"), result.reason_code, ""
            changed = replace(task, state=state, reason_code=reason, resume_phase=phase, version=task.version + 1)
            if (state, reason, phase) != (task.state, task.reason_code, task.resume_phase):
                await tx.apply_changes(ChangeSet(changed))
                await tx.append_event(task_id, TaskEvent("scheduled", {"state": state, "reason_code": reason}))
            else:
                changed = task
            if result.status == "blocked":
                await self._release_unstarted(tx, changed)
        if result.status != "assigned":
            return None
        if changed.plan is None:
            key = (changed.customer_id, changed.task_id)
            work = PlanningWork(changed, result.allocation)
            work.job = asyncio.create_task(self._run_planning(work), name="plan:" + changed.task_id)
            self._planning[key] = work
            self._watch_work(work.job, self._planning, key, work)
            return work.job
        return None

    async def _generate_plan(self, task, snapshot, *, work):
        context = TrustedContext(task.customer_id, task.actor_kind, task.actor_id)
        records, provider_error = (), None
        try:
            candidate = await self.planner.plan(task.goal_text, list(task.answers), snapshot)
            if self.max_plan_steps is not None and len(candidate.steps) > self.max_plan_steps:
                raise PlanError("PLAN_LIMIT_EXCEEDED")
            if candidate.outcome == "clarify":
                changed = replace(task, state="NEEDS_CLARIFICATION", clarification=candidate.question,
                                  clarification_id=str(uuid4()))
            elif candidate.outcome == "unsupported":
                changed = replace(task, state="FAILED", reason_code="UNSUPPORTED")
            else:
                check = await self.preflight.check(context, candidate, snapshot)
                state = {"allow": "READY", "deny": "FAILED", "require_confirmation": "WAITING_APPROVAL",
                         "unavailable": "BLOCKED"}[check.authorization]
                changed = replace(task, state=state, reason_code="" if state == "READY" else "AUTHORIZATION_" + check.authorization.upper(),
                                  snapshot=snapshot, plan=Plan(str(uuid4()), 1, check.validation.steps, check.validation.snapshot_id))
                records = check.records
        except DependencyUnavailable as exc:
            provider_error = exc
            changed = replace(task, state="BLOCKED", reason_code=str(exc), resume_phase="PLANNING")
        except PreflightBlocked as exc:
            changed = replace(task, state="BLOCKED", reason_code=str(exc))
        except PlanError:
            changed = replace(task, state="FAILED", reason_code="PLAN_INVALID")
        if self.ownership is not None and not await self.ownership.healthy():
            changed = replace(task, state="BLOCKED", reason_code="OWNERSHIP_UNAVAILABLE", resume_phase="PLANNING")
            records = ()
        # Once the final write starts, let it finish and report any error; cleanup
        # must not turn a committed provider failure into a silent cancellation.
        work.completing = True
        async with self.store.system_unit_of_work() as tx:
            current = await tx.load_task(task.task_id, for_update=True)
            if await self._abandon_if_disabled(tx, current):
                return
            allocation = await tx.task_allocation(task)
            if (current.version != task.version or current.state != "PLANNING" or allocation is None
                    or allocation.allocation_id != work.allocation.allocation_id or allocation.snapshot.snapshot_id != snapshot.snapshot_id):
                return  # Cancellation/recovery won; never resurrect it or release a later task's slot.
            changed = replace(changed, version=current.version + 1)
            await tx.apply_changes(ChangeSet(changed, decisions=records))
            await tx.append_event(task.task_id, TaskEvent("planned", {"state": changed.state, "reason_code": changed.reason_code}))
            if changed.state != "READY":
                await self._release_unstarted(tx, changed)
        if provider_error:
            raise provider_error

    async def _release_allocation(self, tx, task):
        # Called only with current task facts inside the short state transaction.
        allocation = await tx.task_allocation(task)
        if allocation:
            await tx.release_allocation(allocation)

    async def _release_unstarted(self, tx, task):
        if not task.result_disputed and not any(command.dispatch_state != "PREPARED"
                and command.outcome not in {"SUCCEEDED", "FAILED"} for command in task.commands):
            await self._release_allocation(tx, task)

    async def cancel(self, context, task_id, expected_version, request_key):
        digest = sha256(json.dumps([task_id, expected_version]).encode()).hexdigest()
        async with self.store.system_unit_of_work() as tx:
            existing = await tx.get_request(context.customer_id, context.actor_id, request_key, "cancel")
            task = await tx.load_task(task_id, for_update=True)
            if task.customer_id != context.customer_id or task.actor_id != context.actor_id:
                raise NotFoundError("object not found")
            if existing:
                if existing[0] != digest:
                    raise ConflictError("request key content conflict")
                return await self.view_in_transaction(tx, task)
            if task.version != expected_version:
                raise ConflictError("task version changed")
            if not await self.policy.can_cancel(context, task):
                raise PermissionError("CANCEL_POLICY_UNAVAILABLE")
            if task.state in {"SUCCEEDED", "FAILED", "CANCELLED"}:
                raise ConflictError("task already ended")
            in_flight = next((command for command in task.commands if command.dispatch_state != "PREPARED"
                             and command.outcome not in {"SUCCEEDED", "FAILED"}), None)
            changed = replace(task, state="CANCEL_REQUESTED" if in_flight else "CANCELLED", cancel_requested=True,
                              version=task.version + 1)
            await tx.apply_changes(ChangeSet(changed))
            await tx.put_request(context.customer_id, context.actor_id, request_key, digest, task_id, "cancel")
            await tx.append_event(task_id, TaskEvent("cancel_requested", {"state": changed.state}))
            if not in_flight:
                await self._release_allocation(tx, task)
        if in_flight:
            await self.robot.cancel(in_flight)
        else:
            await self._cancel_invalid_planning()
        return await self.get(context, changed.task_id)

    async def answer(self, context, task_id, clarification_id, answer, expected_version, request_key):
        if not answer.strip() or not request_key:
            raise PlanError("ANSWER_REQUIRED")
        digest = sha256(json.dumps([task_id, clarification_id, answer, expected_version]).encode()).hexdigest()
        async with self.store.unit_of_work(context) as tx:
            existing = await tx.get_request(context.customer_id, context.actor_id, request_key, "answer")
            if existing:
                if existing[0] != digest:
                    raise ConflictError("request key content conflict")
                return await self.view_in_transaction(tx, await tx.load_task(existing[1]))
            task = await tx.load_task(task_id, for_update=True)
            if task.version != expected_version or task.state != "NEEDS_CLARIFICATION" or task.clarification_id != clarification_id:
                raise ConflictError("clarification version changed")
            changed = replace(task, state="RECEIVED", answers=task.answers + (answer,), clarification="",
                              clarification_id="", reason_code="", resume_phase="", version=task.version + 1)
            await tx.apply_changes(ChangeSet(changed))
            await tx.put_request(context.customer_id, context.actor_id, request_key, digest, task_id, "answer")
            await tx.append_event(task_id, TaskEvent("clarification_answered", {"state": changed.state}))
        # A clarification is also durable input, not a request to wait for the model.
        return await self.get(context, changed.task_id)

    async def dispatch_next(self, deployment_id, *, wait_for_send=True):
        if self._closing or self.queue_policy is None or self.ownership is None or not await self.ownership.healthy():
            return
        await self._abandon_disabled_tasks()
        async with self.store.system_unit_of_work() as tx:
            tasks = await tx.list_tasks()
            allocated = set()
            for customer_id in {task.customer_id for task in tasks}:
                allocated.update((item.customer_id, item.task_id) for item in (await tx.allocations(customer_id)).values())
        jobs = []
        for task in self.queue_policy.order(tasks):
            if (task.customer_id, task.task_id) not in allocated:
                continue
            if task.plan is None or task.snapshot is None or task.cancel_requested or task.result_disputed:
                continue
            if task.state not in {"READY", "QUEUED", "RUNNING"} and not (task.state == "BLOCKED" and task.reason_code == "REVALIDATION_REQUIRED"):
                continue
            key = (task.customer_id, task.snapshot.robot_binding_id)
            job = self._dispatches.get(key)
            if job is None:
                job = asyncio.create_task(self._run_dispatch(task), name="dispatch:" + task.task_id)
                self._dispatches[key] = job
                self._watch_work(job, self._dispatches, key, job)
            jobs.append(job)
        if wait_for_send:
            await self._join_work(jobs)

    async def _run_dispatch(self, original):
        try:
            await self._dispatch_task(original)
        except BaseException:
            # A cancelled socket write may have reached the robot. Keep uncertainty
            # and the allocation instead of retrying or declaring physical failure.
            async with self.store.system_unit_of_work() as tx:
                current = await tx.load_task(original.task_id, for_update=True)
                change = uncertain_execution(current)
                if change.task != current:
                    await tx.apply_changes(change)
                    await tx.append_event(current.task_id, TaskEvent("result_unknown", {"reason_code": "SEND_INTERRUPTED"}))
            raise

    async def _dispatch_task(self, task):
        async with self.store.system_unit_of_work() as tx:
            # Another dispatch may have finished after the scheduler's list read.
            task = await tx.load_task(task.task_id)
            resuming_prepared = task.state == "BLOCKED" and task.reason_code == "REVALIDATION_REQUIRED"
            if task.state not in {"READY", "QUEUED", "RUNNING"} and not resuming_prepared:
                return
            if task.cancel_requested or task.result_disputed or task.plan is None or task.snapshot is None:
                return
            if inflight_commands(task):
                # Reports still update observations. Check them only when preparing
                # a new send; an in-flight command must wait for execution evidence.
                return
            context = TrustedContext(task.customer_id, task.actor_kind, task.actor_id)
            allocation = await tx.task_allocation(task)
            if allocation is None or (allocation.customer_id, allocation.task_id, allocation.robot_binding_id) != (
                    task.customer_id, task.task_id, task.snapshot.robot_binding_id):
                return
            approvals = await tx.approvals(task)
        try:
            snapshot = await self.resources.snapshot(context, task.snapshot.robot_binding_id)
            check = await self.preflight.check(context, task.plan, snapshot, "dispatch", approvals)
        except (PlanError, PreflightBlocked, DependencyUnavailable) as exc:
            await self._block_current(task, str(exc))
            return
        if check.authorization != "allow":
            async with self.store.system_unit_of_work() as tx:
                current = await tx.load_task(task.task_id, for_update=True)
                if current.version != task.version:
                    return
                started = any(item.dispatch_state != "PREPARED" for item in current.commands)
                state = "WAITING_APPROVAL" if check.authorization == "require_confirmation" and not started else "BLOCKED"
                reason = "AUTHORIZATION_REQUIRED" if check.authorization == "require_confirmation" else "AUTHORIZATION_" + check.authorization.upper()
                await tx.apply_changes(ChangeSet(replace(current, state=state, reason_code=reason,
                                                        version=current.version + 1), decisions=check.records))
                await tx.append_event(current.task_id, TaskEvent("dispatch_blocked", {"reason_code": reason}))
                await self._release_unstarted(tx, current)
            return
        async with self.store.system_unit_of_work() as tx:
            task = await tx.load_task(task.task_id, for_update=True)
            if task.cancel_requested or task.state in {"SUCCEEDED", "FAILED", "CANCELLED"}:
                return
            if await self._abandon_if_disabled(tx, task):
                return
            binding = await tx.get_binding(task.customer_id, task.snapshot.robot_binding_id)
            reason = ""
            if binding is None or not binding["enabled"] or binding["isolation_reason"]:
                reason = "BINDING_ISOLATED"
            elif snapshot.binding_version and (binding["version"] != snapshot.binding_version
                                               or binding["connection_generation"] != snapshot.generation):
                reason = "REVALIDATION_REQUIRED"
            if reason:
                await tx.apply_changes(ChangeSet(replace(task, state="BLOCKED", reason_code=reason,
                                                        version=task.version + 1)))
                await tx.append_event(task.task_id, TaskEvent("dispatch_blocked", {"reason_code": reason}))
                await self._release_unstarted(tx, task)
                return
            prepared = next((item for item in task.commands if item.dispatch_state == "PREPARED"), None)
            if prepared and not task.cancel_requested:
                change = ChangeSet(replace(task, state="RUNNING", reason_code="", version=task.version + 1), prepared)
            else:
                change = self.executor.prepare(context, None, task)
            if change.command:
                if not prepared:
                    command = replace(change.command, dispatch_generation=binding["connection_generation"],
                                      owner_run_id=self.ownership.run_id)
                    change = replace(change, command=command,
                                     task=replace(change.task, commands=change.task.commands[:-1] + (command,)))
            allocation = await tx.task_allocation(task)
            if change.command is None or allocation is None or allocation.task_id != task.task_id:
                return
            await tx.apply_changes(replace(change, decisions=check.records))
            await tx.append_event(task.task_id, TaskEvent("command_prepared", {"command_id": change.command.command_id}))
        task = change.task
        # Renew checks after PREPARED is committed; external dependencies never wait inside the DB transaction.
        async with self.store.system_unit_of_work() as tx:
            approvals = await tx.approvals(task)
        try:
            snapshot = await self.resources.snapshot(context, task.snapshot.robot_binding_id)
            check = await self.preflight.check(context, task.plan, snapshot, "dispatch", approvals)
        except (PlanError, PreflightBlocked, DependencyUnavailable) as exc:
            await self._block_current(task, str(exc))
            return
        async with self.store.system_unit_of_work() as tx:
            task = await tx.load_task(task.task_id, for_update=True)
            if task.cancel_requested or task.state in {"SUCCEEDED", "FAILED", "CANCELLED"}:
                return
            if await self._abandon_if_disabled(tx, task):
                return
            binding = await tx.get_binding(task.customer_id, task.snapshot.robot_binding_id)
            if snapshot.binding_version and (not binding or binding["version"] != snapshot.binding_version
                                             or binding["connection_generation"] != snapshot.generation):
                await tx.apply_changes(ChangeSet(replace(task, state="BLOCKED", reason_code="REVALIDATION_REQUIRED",
                                                        version=task.version + 1)))
                await tx.append_event(task.task_id, TaskEvent("dispatch_blocked", {"reason_code": "REVALIDATION_REQUIRED"}))
                await self._release_unstarted(tx, task)
                return
            if check.authorization != "allow" or not binding or not binding["enabled"] or binding["isolation_reason"]:
                reason = "AUTHORIZATION_" + check.authorization.upper() if check.authorization != "allow" else "BINDING_ISOLATED"
                await tx.apply_changes(ChangeSet(replace(task, state="BLOCKED", reason_code=reason,
                                                        version=task.version + 1), decisions=check.records))
                await tx.append_event(task.task_id, TaskEvent("dispatch_blocked", {"reason_code": reason}))
                await self._release_unstarted(tx, task)
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
            if await self._abandon_if_disabled(tx, task):
                task = await tx.load_task(task.task_id)
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
            if not binding["enabled"]:
                for previous in related:
                    await self._abandon_task(tx, previous, "ROBOT_DISABLED")
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
            if change.command and change.command.evidence_conflict and not is_abandoned(task):
                await tx.isolate_binding(binding, "EVIDENCE_CONFLICT", event.event_id)
                for other in related:
                    if other.task_id != task.task_id and other.state not in {"SUCCEEDED", "FAILED", "CANCELLED"}:
                        blocked = replace(other, state="BLOCKED", reason_code="EVIDENCE_CONFLICT",
                                          result_disputed=True, version=other.version + 1)
                        await tx.apply_changes(ChangeSet(blocked))
                        await tx.append_event(other.task_id, TaskEvent("binding_isolated", {"reason_code": "EVIDENCE_CONFLICT"}))
            if change.task.state in {"SUCCEEDED", "FAILED", "CANCELLED"}:
                await self._release_allocation(tx, task)
            return await self.view_in_transaction(tx, change.task)

    async def _block_current(self, original, reason, *, resume_phase=""):
        async with self.store.system_unit_of_work() as tx:
            task = await tx.load_task(original.task_id, for_update=True)
            if await self._abandon_if_disabled(tx, task):
                return
            if task.version != original.version:
                return
            await tx.apply_changes(ChangeSet(replace(task, state="BLOCKED", reason_code=reason, resume_phase=resume_phase,
                                                    version=task.version + 1)))
            await tx.append_event(task.task_id, TaskEvent("blocked", {"reason_code": reason}))
            await self._release_unstarted(tx, task)

    async def decide(self, context, task_id, expected_version, decision, request_key, *, plan_id=None):
        if decision not in {"approve", "reject"}:
            raise PlanError("invalid approval decision")
        digest = sha256(json.dumps([task_id, expected_version, decision]).encode()).hexdigest()
        async with self.store.system_unit_of_work() as tx:
            existing = await tx.get_request(context.customer_id, context.actor_id, request_key, "decide")
            if existing:
                if existing[0] != digest:
                    raise ConflictError("request key content conflict")
                return await self.view_in_transaction(tx, await tx.load_task(existing[1]))
            task = await tx.load_task(task_id)
            approvals = await tx.approvals(task) if task.plan else []
        if task.customer_id != context.customer_id:
            raise NotFoundError("object not found")
        if not await self.policy.can_approve(context, task):
            raise PermissionError("APPROVAL_DENIED")
        if plan_id is not None and (task.plan is None or task.plan.plan_id != plan_id):
            raise ConflictError("plan version changed")
        if task.version != expected_version:
            raise ConflictError("task version changed")
        if task.plan is None or task.state not in {"WAITING_APPROVAL", "BLOCKED"} \
                or task.reason_code in {"RESULT_UNKNOWN", "EVIDENCE_CONFLICT", "UNTRACKED_EXECUTION"}:
            raise ConflictError("task does not await approval")
        records = ()
        if decision == "approve":
            ttl = getattr(self.policy, "approval_ttl_seconds", None)
            if ttl is None or ttl <= 0 or not self.policy.version:
                raise PreflightBlocked("APPROVAL_VALIDITY_UNAVAILABLE")
            now = datetime.now(timezone.utc)
            evidence = {"customer_id": task.customer_id, "plan_id": task.plan.plan_id, "plan_revision": task.plan.revision,
                        "scope": tuple(step.step_id for step in task.plan.steps), "approver_id": context.actor_id,
                        "policy_version": self.policy.version, "issued_at": now, "expires_at": now + timedelta(seconds=ttl),
                        "revoked_at": None}
            snapshot = await self.resources.snapshot(context, task.snapshot.robot_binding_id)
            check = await self.preflight.check(context, task.plan, snapshot, "approval", approvals + [evidence])
            state = "READY" if check.authorization == "allow" else "BLOCKED"
            reason = "" if state == "READY" else "AUTHORIZATION_" + check.authorization.upper()
            records = check.records + (Decision(str(uuid4()), "approval", "approve", self.policy.version, detail=evidence),)
        else:
            state, reason = "FAILED", "APPROVAL_REJECTED"
        async with self.store.system_unit_of_work() as tx:
            existing = await tx.get_request(context.customer_id, context.actor_id, request_key, "decide")
            if existing:
                if existing[0] != digest:
                    raise ConflictError("request key content conflict")
                return await self.view_in_transaction(tx, await tx.load_task(existing[1]))
            current = await tx.load_task(task_id, for_update=True)
            if current.version != expected_version:
                raise ConflictError("task version changed")
            changed = replace(current, state=state, reason_code=reason, version=current.version + 1)
            await tx.apply_changes(ChangeSet(changed, decisions=records))
            await tx.put_request(context.customer_id, context.actor_id, request_key, digest, task_id, "decide")
            await tx.append_event(task_id, TaskEvent("decision", {"decision": decision, "state": state}))
        if changed.state == "READY":
            await self.schedule_pending(self.store.deployment_id, task_id=task_id)
            return await self.get(context, task_id)
        return await self.get(context, changed.task_id)

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
        active = tuple(command for task in tasks for command in inflight_commands(task))
        tracked = any(command.command_id == command_id for command in active)
        has_active = bool(active)
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
            if not stored["enabled"]:
                for task in tasks:
                    await self._abandon_task(tx, task, "ROBOT_DISABLED")
            else:
                await self._observe_busy(tx, stored, generation, frame, tasks)

    async def on_robot_disconnected(self, binding, generation):
        async with self.store.system_unit_of_work() as tx:
            stored = await tx.get_binding(binding["customer_id"], binding["binding_id"])
            if stored is None or stored["connection_generation"] != generation:
                return
            for task in await tx.tasks_for_binding(binding):
                if not stored["enabled"]:
                    await self._abandon_task(tx, task, "ROBOT_DISABLED")
                    continue
                if task.state == "PLANNING" and not task.commands:
                    changed = replace(task, state="BLOCKED", reason_code="REVALIDATION_REQUIRED",
                                      resume_phase="PLANNING", version=task.version + 1)
                    await tx.apply_changes(ChangeSet(changed))
                    await tx.append_event(task.task_id, TaskEvent("planning_interrupted", {"reason_code": changed.reason_code}))
                    await self._release_unstarted(tx, changed)
                    continue
                change = uncertain_execution(task)
                if change.task != task:
                    await tx.apply_changes(change)
                    await tx.append_event(task.task_id, TaskEvent("result_unknown", {"reason_code": "RESULT_UNKNOWN"}))
            await tx.update_observations(binding, generation, {"connected": False})
        await self._cancel_invalid_planning()

    async def _abandon_disabled_tasks(self):
        # Scope by durable allocation or plan, never by the robot currently online.
        async with self.store.system_unit_of_work() as tx:
            for original in await tx.list_tasks():
                if original.state not in {"SUCCEEDED", "FAILED", "CANCELLED"}:
                    task = await tx.load_task(original.task_id, for_update=True)
                    await self._abandon_if_disabled(tx, task)

    async def _abandon_if_disabled(self, tx, task):
        if task.state in {"SUCCEEDED", "FAILED", "CANCELLED"}:
            return False
        allocation = await tx.task_allocation(task)
        binding_id = allocation.robot_binding_id if allocation else (task.snapshot.robot_binding_id if task.snapshot else None)
        if binding_id is None:
            return False
        binding = await tx.get_binding(task.customer_id, binding_id)
        if binding is None or binding["enabled"]:
            return False
        await self._abandon_task(tx, task, "ROBOT_DISABLED")
        return True

    async def _abandon_task(self, tx, task, reason):
        """End one current task; keep evidence and release only its exact allocation."""
        if task.state in {"SUCCEEDED", "FAILED", "CANCELLED"}:
            return
        uncertain = uncertain_execution(task).task
        ended = replace(uncertain, state="CANCELLED", reason_code=reason,
                        cancel_requested=True, resume_phase="", version=task.version + 1)
        await tx.apply_changes(ChangeSet(ended))
        await tx.append_event(task.task_id, TaskEvent("task_abandoned", {"reason_code": reason}))
        await self._release_allocation(tx, task)

    async def recover(self, deployment_id):
        # Startup must commit this before accepting requests, even with dispatch off.
        if self.ownership is not None and not await self.ownership.healthy():
            raise DependencyUnavailable("OWNERSHIP_UNAVAILABLE")
        async with self.store.system_unit_of_work() as tx:
            for original in await tx.list_tasks():
                task = await tx.load_task(original.task_id, for_update=True)
                await self._abandon_task(tx, task, "RESTART_TASK_LOST")

    async def expire_commands(self, deployment_id):
        if self.command_timeout_seconds is None:
            return
        async with self.store.system_unit_of_work() as tx:
            for task_id in await tx.expired_task_ids(self.command_timeout_seconds):
                task = await tx.load_task(task_id, for_update=True)
                change = uncertain_execution(task)
                if change.task != task:
                    await tx.apply_changes(change)
                    await tx.append_event(task_id, TaskEvent("result_unknown", {"reason_code": "COMMAND_TIMEOUT"}))

    async def may_write(self, command):
        if self.ownership is None or not await self.ownership.healthy() or command.owner_run_id != self.ownership.run_id:
            return False
        async with self.store.system_unit_of_work() as tx:
            try:
                task = await tx.load_task(command.task_id, for_update=True)
            except NotFoundError:
                return False
            allocation = await tx.task_allocation(task)
            if task.customer_id != command.customer_id or allocation is None or (
                    allocation.customer_id, allocation.task_id, allocation.robot_binding_id) != (
                    command.customer_id, command.task_id, command.robot_binding_id):
                return False
            approvals = await tx.approvals(task)
        context = TrustedContext(task.customer_id, task.actor_kind, task.actor_id)
        try:
            snapshot = await self.resources.snapshot(context, command.robot_binding_id)
            check = await self.preflight.check(context, task.plan, snapshot, "write", approvals)
        except (PlanError, PreflightBlocked, DependencyUnavailable):
            return False
        if check.authorization != "allow":
            return False
        async with self.store.system_unit_of_work() as tx:
            current = await tx.load_task(task.task_id, for_update=True)
            if current.version != task.version or current.cancel_requested:
                return False
            binding = await tx.get_binding(command.customer_id, command.robot_binding_id)
            if snapshot.binding_version and (not binding or binding["version"] != snapshot.binding_version
                                             or binding["connection_generation"] != snapshot.generation):
                return False
            if snapshot.valid_until is not None and datetime.now(timezone.utc) >= snapshot.valid_until:
                return False
            latest = await tx.approvals(current)
            if latest != approvals:
                return False
            if check.records[-1].detail["base_result"] == "require_confirmation" and not any(approval_covers(
                    evidence, customer_id=task.customer_id, plan_id=task.plan.plan_id, revision=task.plan.revision,
                    scope=tuple(step.step_id for step in task.plan.steps), policy_version=self.policy.version,
                    now=datetime.now(timezone.utc)) for evidence in latest):
                return False
            if check.records[-1].rule_or_policy_version != self.policy.version:
                return False
            stored = next((item for item in current.commands if item.command_id == command.command_id), None)
            return (binding is not None and binding["enabled"] and not binding["isolation_reason"]
                    and binding["connection_generation"] == command.dispatch_generation
                    and stored is not None and stored.dispatch_state == "DISPATCHING"
                    and stored.outcome not in {"SUCCEEDED", "FAILED", "RESULT_UNKNOWN"} and current.state == "RUNNING")

    async def home_tasks(self, context):
        async with self.store.unit_of_work(context) as tx:
            rows = await tx.home_task_rows()
        result = []
        for row in rows:
            item = dict(row)
            labels = json.loads(item.pop("labels") or "{}")
            steps = json.loads(item.pop("steps") or "[]")
            item["plan_steps"] = [{"step_id": step["step_id"],
                                   "description": step_description(step["action"], step["args"], labels)}
                                  for step in steps]
            item["result_summary"] = json.loads(item.pop("commands") or "[]")
            result.append(item)
        return result

    async def history(self, context, *, limit, before):
        async with self.store.unit_of_work(context) as tx:
            rows, state_counts = await tx.owner_history(limit + 1, before)
        page = rows[:limit]
        return {"tasks": page, "next_before": page[-1]["queue_seq"] if len(rows) > limit else None,
                "state_counts": state_counts}

    async def _can_view_task(self, context, tx, task):
        if task.customer_id != context.customer_id:
            return False
        if await tx.task_visible(task.task_id):
            return True
        return "approver" in context.roles and await self.policy.can_approve(context, task)

    async def get(self, context, task_id):
        async with self.store.unit_of_work(context) as tx:
            task = await tx.load_task(task_id, actor_scope=False)
            if not await self._can_view_task(context, tx, task):
                raise NotFoundError(task_id)
            return await self.view_in_transaction(tx, task)

    async def events(self, context, task_id, after):
        if after < 0:
            raise PlanError("invalid event cursor")
        async with self.store.unit_of_work(context) as tx:
            task = await tx.load_task(task_id, actor_scope=False)
            if not await self._can_view_task(context, tx, task):
                raise NotFoundError(task_id)
            events = await tx.task_events(task_id, after, 100)
        return {"events": events, "next_seq": events[-1]["seq"] if events else after}
