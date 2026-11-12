"""Development checks of real policy/control functions, not end-to-end robot acceptance.

Run with PYTHONPATH=src python -B -m unittest discover -s tests/unit -p test_task_control_policy.py.
No database, model, transport, or business-rule substitute is installed by these checks.
"""

from dataclasses import replace
import unittest

from robot_agent.adapters.identity import ConfiguredIdentity
from robot_agent.adapters.policy import ConfiguredPolicy
from robot_agent.application.task_service import TaskService
from robot_agent.domain.models import Command, ContextSnapshot, Plan, PlanStep, Task, TrustedContext


class TaskControlPolicyTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.identity = ConfiguredIdentity(
            users={
                "owner-token": {"customer_id": "c", "actor_id": "owner", "roles": ["user", "approver"]},
                "peer-token": {"customer_id": "c", "actor_id": "peer", "roles": ["user"]},
                "other-token": {"customer_id": "other", "actor_id": "owner", "roles": ["approver"]},
            },
            robots={
                "robot-token": {"customer_id": "c", "binding_id": "robot", "wire_robot_id": "wire-robot"},
                "peer-robot-token": {"customer_id": "c", "binding_id": "peer-robot"},
                "other-robot-token": {"customer_id": "other", "binding_id": "other-robot"},
            },
        )
        self.policy = ConfiguredPolicy(self.identity, version="policy:v1", actions={}, approval_ttl_seconds=120)
        self.user = TrustedContext("c", "user", "owner")
        self.task = Task("turn", "c", "robot", "robot", "bring a drink", conversation_id="chat")
        # Only interaction() is exercised here. No platform/runtime is started.
        self.service = TaskService(store=None, planner=None, robot=None, resources=None,
                                   policy=self.policy, catalog=None)
        self.snapshot = ContextSnapshot("c", "robot", 1, frozenset(), {}, {}, {}, "snapshot", frozenset())
        self.plan = Plan("plan", 1, (PlanStep("step", "move_to", {"location": "desk"}, (), ()),), "snapshot")
        self.command = Command("command", "turn", "c", "unit", "plan", "step", "robot",
                               "move_to", {"location": "desk"}, 1, dispatch_state="SENT", outcome="RUNNING")

    async def test_customer_user_can_approve_and_control_robot_origin(self):
        user = await self.identity.resolve_web("owner-token")
        self.assertTrue(await self.policy.can_approve(user, self.task))
        self.assertTrue(await self.policy.can_control(user, self.task))
        self.assertTrue(await self.policy.can_cancel(user, self.task))
        self.assertEqual((self.task.actor_kind, self.task.actor_id), ("robot", "robot"))

    async def test_controls_preserve_identity_and_customer_boundaries(self):
        cases = [
            (self.user, self.task, True),
            (self.user, replace(self.task, actor_kind="user", actor_id="owner"), True),
            (self.user, replace(self.task, actor_kind="user", actor_id="peer"), False),
            (self.user, replace(self.task, customer_id="other", actor_kind="user", actor_id="owner"), False),
            (self.user, replace(self.task, customer_id="other", actor_id="other-robot"), False),
            (self.user, replace(self.task, actor_id="unregistered"), False),
            (self.user, replace(self.task, actor_id="other-robot"), False),
            (TrustedContext("other", "user", "owner"), self.task, False),
            (TrustedContext("c", "robot", "robot"), self.task, True),
            (TrustedContext("c", "robot", "peer-robot"), self.task, False),
            (TrustedContext("c", "robot", "robot"), replace(self.task, actor_kind="user"), False),
            (TrustedContext("c", "unknown", "robot"), self.task, False),
        ]
        for context, task, allowed in cases:
            with self.subTest(context=context, owner=(task.customer_id, task.actor_kind, task.actor_id)):
                self.assertEqual(await self.policy.can_control(context, task), allowed)
                self.assertEqual(await self.policy.can_cancel(context, task), allowed)

    async def test_approver_cannot_take_over_another_users_task(self):
        private_task = replace(self.task, actor_kind="user", actor_id="peer")
        self.assertTrue(await self.policy.can_approve(self.user, private_task))
        self.assertFalse(await self.policy.can_control(self.user, private_task))
        controls = await self.service.interaction(self.user, replace(private_task, state="FAILED"))
        self.assertFalse(controls["can_message"])

    async def test_removed_identities_do_not_retain_control(self):
        del self.identity.users["owner-token"]
        self.assertFalse(await self.policy.can_control(self.user, self.task))
        self.identity.users["owner-token"] = {"customer_id": "c", "actor_id": "owner", "roles": ["user"]}
        del self.identity.robots["robot-token"]
        self.assertFalse(await self.policy.can_control(self.user, self.task))
        self.assertFalse(await self.policy.can_cancel(self.user, self.task))

    async def test_running_robot_task_has_stop_control(self):
        task = replace(self.task, state="RUNNING", snapshot=self.snapshot, plan=self.plan,
                       commands=(self.command,))
        controls = await self.service.interaction(self.user, task)
        self.assertTrue(controls["can_stop"])
        self.assertTrue(controls["can_stop_execution"])
        self.assertFalse(controls["can_message"])
        stranger = await self.service.interaction(TrustedContext("other", "user", "owner"), task)
        self.assertFalse(stranger["can_stop"])
        self.assertFalse(stranger["can_stop_execution"])

    async def test_failure_with_definitive_receipt_allows_followup(self):
        task = replace(self.task, state="BLOCKED", reason_code="COMMAND_TIMEOUT", advancement_stopped=True,
                       plan=self.plan, snapshot=self.snapshot, commands=(replace(self.command, outcome="FAILED"),))
        controls = await self.service.interaction(self.user, task)
        self.assertEqual(controls["phase"], "idle")
        self.assertTrue(controls["can_message"])
        for state in ["FAILED", "CANCELLED", "SUCCEEDED", "COMPLETED_WITH_SKIPS", "NEEDS_CLARIFICATION"]:
            with self.subTest(state=state):
                controls = await self.service.interaction(self.user, replace(self.task, state=state))
                self.assertTrue(controls["can_message"])

    async def test_unknown_result_still_blocks_new_turn(self):
        task = replace(self.task, state="BLOCKED", reason_code="COMMAND_TIMEOUT", advancement_stopped=True,
                       plan=self.plan, snapshot=self.snapshot,
                       commands=(replace(self.command, outcome="RESULT_UNKNOWN"),))
        controls = await self.service.interaction(self.user, task)
        self.assertFalse(controls["can_message"])
        self.assertTrue(controls["can_stop"])
        self.assertFalse(controls["can_stop_execution"])

    async def test_pause_waits_for_receipt_then_allows_resume(self):
        task = replace(self.task, state="PAUSE_REQUESTED", plan=self.plan, snapshot=self.snapshot,
                       pause_step_id="step", pause_command_id="command", pause_result={"status": "SENT"},
                       commands=(self.command,))
        controls = await self.service.interaction(self.user, task)
        self.assertFalse(controls["can_resume_execution"])
        task = replace(task, state="PAUSED", commands=(replace(self.command, outcome="FAILED"),))
        controls = await self.service.interaction(self.user, task)
        self.assertTrue(controls["can_resume_execution"])
        self.assertTrue(controls["can_end_execution"])
        self.assertFalse(controls["can_message"])


if __name__ == "__main__":
    unittest.main()
