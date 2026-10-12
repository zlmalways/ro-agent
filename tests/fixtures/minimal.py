"""Explicit P0 substitutes. Not importable from production adapters or app assembly."""

from contextlib import asynccontextmanager
from copy import deepcopy

from robot_agent.application.task_service import TaskService
from robot_agent.domain.models import ActionCatalog, CandidatePlan, ContextSnapshot, SendObservation


class MemoryTestStore:
    def __init__(self):
        self.tasks = {}
        self.request_keys = {}
        self.slot = None

    @asynccontextmanager
    async def unit_of_work(self, context):
        backup = deepcopy((self.tasks, self.request_keys, self.slot))
        try:
            yield self
        except BaseException:
            self.tasks, self.request_keys, self.slot = backup
            raise

    async def insert_task(self, task):
        self.tasks[task.task_id] = deepcopy(task)

    async def load_task(self, task_id):
        return deepcopy(self.tasks[task_id])

    async def save_task(self, task):
        self.tasks[task.task_id] = deepcopy(task)

    async def list_tasks(self):
        return deepcopy(list(self.tasks.values()))

    async def get_request(self, customer_id, actor_id, key):
        return self.request_keys.get((customer_id, actor_id, key))

    async def put_request(self, customer_id, actor_id, key, digest, task_id):
        self.request_keys[customer_id, actor_id, key] = (digest, task_id)

    async def claim_slot(self, task_id):
        if self.slot not in (None, task_id):
            return False
        self.slot = task_id
        return True

    async def release_slot(self, task_id):
        if self.slot == task_id:
            self.slot = None


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


class TestResources:
    async def snapshot(self, context):
        return ContextSnapshot(
            customer_id=context.customer_id, robot_binding_id="test-binding", generation=1,
            skills=frozenset({"move_to", "vla_skill"}),
            waypoints={"test-table": "environment:v1:table"},
            brain_skills={"test-pick": "capabilities:v1:pick"},
            readiness={"nav_server_ready": True, "brain_server_ready": True, "brain_engine": "ready"},
            snapshot_id="test-snapshot", allowed_actions=frozenset({"move_to", "vla_skill"}),
        )


class TestPolicy:
    def __init__(self, result):
        self.result = result

    async def evaluate(self, context, plan, approvals):
        return self.result


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
    )
    return service, robot, store
