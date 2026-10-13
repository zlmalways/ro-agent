"""Unknown transport facts never replace physical terminal evidence."""

from dataclasses import replace

import pytest

from robot_agent.domain.models import RobotEvent, TaskInput, TrustedContext
from tests.fixtures.minimal import build_service


CONTEXT = TrustedContext("test-customer", "user", "test-user")


@pytest.mark.parametrize("state,expected", [("PREPARED", "NONE"), ("DISPATCHING", "RESULT_UNKNOWN"), ("SENT", "RESULT_UNKNOWN")])
async def test_dispatch_crash_window_unknown(state, expected):
    service, robot, store = build_service()
    view = await service.submit(CONTEXT, TaskInput("req", "web", "test", ""), "key")
    await service.dispatch_next("test-deployment")
    task = store.tasks[view.task_id]
    command = replace(task.commands[0], dispatch_state=state)
    store.tasks[view.task_id] = replace(task, commands=(command,))
    await service.recover("test-deployment")
    recovered = store.tasks[view.task_id]
    assert recovered.commands[0].outcome == expected
    assert recovered.reason_code == ("RESULT_UNKNOWN" if expected != "NONE" else "REVALIDATION_REQUIRED")
    if state != "PREPARED":
        await service.dispatch_next("test-deployment")
        assert len(robot.sent) == 1


async def test_send_error_after_early_success_cannot_overwrite_result():
    service, robot, _ = build_service()
    view = await service.submit(CONTEXT, TaskInput("req", "web", "test", ""), "key")

    async def failed_send_with_feedback(command):
        robot.sent.append(command)
        await service.on_robot_event(RobotEvent("early", CONTEXT.customer_id, "test-binding", 1, command.command_id, "succeeded"))
        raise OSError("test write failure after accepted frame")

    robot.send = failed_send_with_feedback
    await service.dispatch_next("test-deployment")
    result = await service.get(CONTEXT, view.task_id)
    assert result.result_summary[0]["outcome"] == "SUCCEEDED"


async def test_running_does_not_resolve_unknown():
    service, robot, store = build_service()
    view = await service.submit(CONTEXT, TaskInput("req", "web", "test", ""), "key")
    await service.dispatch_next("test-deployment")
    await service.recover("test-deployment")
    command = robot.sent[0]
    await service.on_robot_event(RobotEvent("running", CONTEXT.customer_id, "test-binding", 1, command.command_id, "running"))
    assert store.tasks[view.task_id].commands[0].outcome == "RESULT_UNKNOWN"
    assert store.tasks[view.task_id].state == "BLOCKED"
