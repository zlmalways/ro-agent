"""Explicit P0 substitutes. Not importable from production adapters or app assembly."""

from contextlib import asynccontextmanager
from copy import deepcopy
import asyncio
from uuid import uuid4

from robot_agent.application.task_service import TaskService
from robot_agent.domain.models import ActionCatalog, CandidatePlan, ConflictError, ContextSnapshot, RobotAllocation, SendObservation


class MemoryTestStore:
    def __init__(self):
        self.tasks = {}
        self.request_keys = {}
        self.slots = {}
        self.deployment_id = "test-deployment"
        self.events = []
        self.decisions = []
        self.robot_events = {}
        self.lock = asyncio.Lock()

    @asynccontextmanager
    async def unit_of_work(self, context):
        async with self.lock:
            backup = deepcopy((self.tasks, self.request_keys, self.slots, self.events, self.decisions, self.robot_events))
            try:
                yield self
            except BaseException:
                self.tasks, self.request_keys, self.slots, self.events, self.decisions, self.robot_events = backup
                raise

    def system_unit_of_work(self):
        return self.unit_of_work(None)

    async def insert_task(self, task):
        self.tasks[task.task_id] = deepcopy(task)

    async def load_task(self, task_id, *, for_update=False, actor_scope=True):
        return deepcopy(self.tasks[task_id])

    async def save_task(self, task):
        self.tasks[task.task_id] = deepcopy(task)

    async def apply_changes(self, changes):
        await self.save_task(changes.task)
        self.decisions.extend(deepcopy(changes.decisions))
        if changes.event:
            self.robot_events[changes.event.event_id] = deepcopy(changes.event)

    async def event_seen(self, event):
        return event.event_id in self.robot_events

    async def append_event(self, task_id, value):
        self.events.append((task_id, deepcopy(value)))

    async def find_event_task(self, event):
        return next((deepcopy(task) for task in self.tasks.values() if task.customer_id == event.customer_id
                     and any(command.command_id == event.cmd_id for command in task.commands)), None)

    async def get_binding(self, customer_id, binding_id):
        if customer_id == "test-customer" and binding_id == "test-binding":
            return {"customer_id": customer_id, "binding_id": binding_id, "enabled": True,
                    "isolation_reason": "", "connection_generation": 1}
        return None

    async def record_diagnostic(self, binding, generation, event_id, frame):
        pass

    async def tasks_for_binding(self, binding):
        return [deepcopy(task) for task in self.tasks.values() if task.customer_id == binding["customer_id"]
                and task.snapshot is not None and task.snapshot.robot_binding_id == binding["binding_id"]]

    async def list_tasks(self):
        return deepcopy(list(self.tasks.values()))

    async def get_request(self, customer_id, actor_id, key, operation="submit"):
        return self.request_keys.get((customer_id, actor_id, operation, key))

    async def put_request(self, customer_id, actor_id, key, digest, task_id, operation="submit"):
        self.request_keys[customer_id, actor_id, operation, key] = (digest, task_id)

    async def approvals(self, task):
        return [dict(decision.detail) for decision in self.decisions if decision.kind == "approval"
                and decision.detail["plan_id"] == task.plan.plan_id]

    @property
    def slot(self):
        assert len(self.slots) <= 1, "single-robot test used for a multi-robot case"
        return next(iter(self.slots.values()), None)

    async def task_allocation_binding(self, task):
        allocation = await self.task_allocation(task)
        return allocation.robot_binding_id if allocation else None

    async def allocations(self, customer_id):
        return {binding: deepcopy(value) for (customer, binding), value in self.slots.items() if customer == customer_id}

    async def task_allocation(self, task):
        return next((deepcopy(value) for value in self.slots.values()
                     if value.customer_id == task.customer_id and value.task_id == task.task_id), None)

    async def prior_waiters(self, task):
        result = []
        for prior in self.tasks.values():
            if prior.task_id == task.task_id:
                break
            if prior.customer_id == task.customer_id and prior.state in {"RECEIVED", "QUEUED", "READY"}:
                result.append(deepcopy(prior))
        return result

    async def claim_slot(self, deployment_id, task, snapshot):
        key = (task.customer_id, snapshot.robot_binding_id)
        if key in self.slots or await self.task_allocation(task) or snapshot.customer_id != task.customer_id:
            raise ConflictError("allocation scope or occupied robot")
        allocation = RobotAllocation(task.customer_id, task.task_id, snapshot.robot_binding_id, snapshot, str(uuid4()))
        self.slots[key] = allocation
        return allocation

    async def release_allocation(self, allocation):
        key = (allocation.customer_id, allocation.robot_binding_id)
        if self.slots.get(key) == allocation:
            del self.slots[key]


class TestPlanner:
    def __init__(self, value):
        self.value = value

    async def plan(self, goal, answers, snapshot):
        return CandidatePlan.from_dict(deepcopy(self.value))


class TestRobot:
    def __init__(self):
        self.sent = []

    async def send(self, command):
        self.sent.append(deepcopy(command))
        return SendObservation(True)

    async def cancel(self, command):
        return SendObservation(True)


class TestResources:
    async def list_robots(self, context):
        from robot_agent.domain.models import RobotSummary
        return [RobotSummary("test-binding", "test-robot", True, True, "", 1)]

    async def robot_detail(self, context, binding_id):
        from robot_agent.domain.models import RobotDetail
        return RobotDetail((await self.list_robots(context))[0], await self.snapshot(context, binding_id), {}, None, None)

    async def online_robot_details(self, context):
        return [await self.snapshot(context)]

    async def snapshot(self, context, binding_id=None):
        generation = 1
        # These tests simulate resources, but must use the current connection
        # generation when they exercise the real PostgreSQL/WS adapters.
        if getattr(self, "store", None) is not None:
            async with self.store.system_unit_of_work() as tx:
                binding = await tx.get_binding(context.customer_id, binding_id or "test-binding")
            if binding:
                generation = binding["connection_generation"]
        return ContextSnapshot(
            customer_id=context.customer_id, robot_binding_id="test-binding", generation=generation,
            skills=frozenset({"move_to", "vla_skill"}),
            waypoints={"test-table": "environment:v1:table"},
            brain_skills={"test-pick": "capabilities:v1:pick"},
            readiness={"busy": False, "nav_server_ready": True, "brain_server_ready": True, "brain_engine": "ready"},
            snapshot_id="test-snapshot", allowed_actions=frozenset({"move_to", "vla_skill"}),
        )


class TestPolicy:
    version = "test-policy:v1"
    approval_ttl_seconds = 300

    async def can_approve(self, context, task):
        return context.customer_id == task.customer_id

    async def can_cancel(self, context, task):
        return context.customer_id == task.customer_id and context.actor_id == task.actor_id
    def __init__(self, result):
        self.result = result

    async def evaluate(self, context, plan, approvals):
        return self.result


class TestQueuePolicy:
    """Fixture-only ordering; not a product-policy default."""

    def order(self, tasks):
        return list(tasks)


class TestOwnership:
    run_id = "test-owner-run"

    async def healthy(self):
        return True


def build_service(plan=None, policy_result="allow"):
    if plan is None:
        plan = {"outcome": "plan", "steps": [
            {"step_id": "s1", "action": "move_to", "args": {"location": "test-table"}, "depends_on": []},
            {"step_id": "s2", "action": "vla_skill", "args": {"skill_id": "test-pick"}, "depends_on": ["s1"]},
        ]}
    store, robot = MemoryTestStore(), TestRobot()
    service = TaskService(
        store=store, planner=TestPlanner(plan), robot=robot, resources=TestResources(),
        policy=TestPolicy(policy_result),
        catalog=ActionCatalog({"move_to": ("location",), "vla_skill": ("skill_id",)}),
        queue_policy=TestQueuePolicy(), ownership=TestOwnership(),
    )
    return service, robot, store
