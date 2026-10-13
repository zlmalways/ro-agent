"""P0 only: real business code with explicitly isolated external test doubles."""

import pytest

from robot_agent.domain.models import RobotEvent, TaskInput, TrustedContext
from tests.fixtures.minimal import build_service


CONTEXT = TrustedContext("test-customer", "user", "test-user")


@pytest.mark.asyncio
async def test_minimal_flow_completes_from_matching_feedback():
    service, robot, store = build_service()
    view = await service.submit(CONTEXT, TaskInput("req", "web", "test: fetch cup", ""), "key")
    assert view.state == "READY"
    assert robot.sent == []
    await service.dispatch_next("test-deployment")
    assert len(robot.sent) == 1
    first = robot.sent[0]
    assert first.wire_skill == "move_to"
    assert first.wire_args == {"location": "test-table"}
    await service.on_robot_event(RobotEvent("e1", "test-customer", "test-binding", 1, first.command_id, "running"))
    await service.dispatch_next("test-deployment")
    assert len(robot.sent) == 1
    await service.on_robot_event(RobotEvent("e2", "test-customer", "test-binding", 1, first.command_id, "succeeded"))
    await service.dispatch_next("test-deployment")
    assert len(robot.sent) == 2
    second = robot.sent[1]
    assert second.wire_skill == "vla_skill"
    assert second.wire_args == {"skill_id": "test-pick"}
    await service.on_robot_event(RobotEvent("e3", "test-customer", "test-binding", 1, second.command_id, "succeeded"))
    final = await service.get(CONTEXT, view.task_id)
    assert final.state == "SUCCEEDED"
    assert [row["outcome"] for row in final.result_summary] == ["SUCCEEDED", "SUCCEEDED"]
    assert [row["command_id"] for row in final.result_summary] == [first.command_id, second.command_id]


@pytest.mark.asyncio
@pytest.mark.parametrize("action,args", [
    ("grasp", {}), ("move_to", {"location": "imaginary"}),
    ("vla_skill", {"skill_id": "imaginary"}), ("move_to", {"location": "test-table", "approval": "fake"}),
])
async def test_invalid_plan_never_dispatches(action, args):
    plan = {"outcome": "plan", "steps": [{"step_id": "s1", "action": action, "args": args, "depends_on": []}]}
    service, robot, _ = build_service(plan)
    view = await service.submit(CONTEXT, TaskInput("req", "web", "test", ""), "key")
    assert view.state == "FAILED"
    await service.dispatch_next("test-deployment")
    assert robot.sent == []


@pytest.mark.asyncio
async def test_clarification_never_dispatches():
    service, robot, _ = build_service({"outcome": "clarify", "steps": [], "question": "Which test table?", "required_slots": ["location"]})
    view = await service.submit(CONTEXT, TaskInput("req", "web", "test", ""), "key")
    assert view.state == "NEEDS_CLARIFICATION"
    await service.dispatch_next("test-deployment")
    assert robot.sent == []


@pytest.mark.asyncio
async def test_denied_policy_never_dispatches():
    service, robot, _ = build_service(policy_result="deny")
    view = await service.submit(CONTEXT, TaskInput("req", "web", "test", ""), "key")
    assert view.state == "FAILED"
    await service.dispatch_next("test-deployment")
    assert robot.sent == []


@pytest.mark.asyncio
async def test_wrong_robot_feedback_does_not_finish_task():
    service, robot, _ = build_service()
    view = await service.submit(CONTEXT, TaskInput("req", "web", "test", ""), "key")
    await service.dispatch_next("test-deployment")
    command = robot.sent[0]
    await service.on_robot_event(RobotEvent("forged", "other-customer", "test-binding", 1, command.command_id, "succeeded"))
    assert (await service.get(CONTEXT, view.task_id)).state == "RUNNING"
    assert len(robot.sent) == 1


async def test_explicit_plan_limit_rejects_long_candidate():
    service, robot, _ = build_service()
    service.max_plan_steps = 1
    result = await service.submit(CONTEXT, TaskInput("req", "web", "test", ""), "key")
    assert result.state == "FAILED"
    await service.dispatch_next("test-deployment")
    assert robot.sent == []
