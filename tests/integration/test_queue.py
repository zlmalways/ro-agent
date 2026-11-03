import pytest

from robot_agent.runtime import SubmissionOrderQueue

from robot_agent.domain.models import RobotEvent, TaskInput, TrustedContext


pytestmark = pytest.mark.integration
CONTEXT = TrustedContext("test-customer", "user", "test-user")
INPUT = TaskInput("req", "web", "test", "")


async def test_missing_queue_policy_never_dequeues(persisted_service):
    service, robot = persisted_service
    service.queue_policy = None
    view = await service.submit(CONTEXT, INPUT, "key")
    await service.dispatch_next("test-deployment")
    assert robot.sent == []
    assert (await service.get(CONTEXT, view.task_id)).result_summary == ()


async def test_expired_approval_waits_and_does_not_repeat_completed_steps(persisted_service):
    service, robot = persisted_service
    view = await service.submit(CONTEXT, INPUT, "key")
    await service.dispatch_next("test-deployment")
    await service.on_robot_event(RobotEvent("success", CONTEXT.customer_id, "test-binding", 1,
                                          robot.sent[0].command_id, "succeeded"))
    service.policy.result = "require_confirmation"
    await service.dispatch_next("test-deployment")
    blocked = await service.get(CONTEXT, view.task_id)
    assert blocked.reason_code == "AUTHORIZATION_REQUIRED"
    assert blocked.state == "BLOCKED"
    assert len(robot.sent) == 1
    assert blocked.result_summary[0]["outcome"] == "SUCCEEDED"


@pytest.mark.parametrize("cancel_first", [False, True], ids=["first-finishes", "first-cancelled"])
async def test_two_tasks_release_robot_in_submission_order(persisted_service, cancel_first):
    service, robot = persisted_service
    service.queue_policy = SubmissionOrderQueue()
    first = await service.submit(CONTEXT, INPUT, "first")
    second = await service.submit(CONTEXT, INPUT, "second")

    async def finish_step(task_id, skill, terminal_state):
        await service.dispatch_next("test-deployment")
        cmd = robot.sent[-1]
        assert (cmd.task_id, cmd.wire_skill) == (task_id, skill)
        count = len(robot.sent)
        await service.dispatch_next("test-deployment")
        assert len(robot.sent) == count
        if terminal_state == "failed":
            view = await service.get(CONTEXT, task_id)
            requested = await service.cancel(CONTEXT, task_id, view.version, "cancel-first")
            assert requested.state == "CANCEL_REQUESTED"
            await service.dispatch_next("test-deployment")
            assert len(robot.sent) == count
        await service.on_robot_event(RobotEvent(cmd.command_id, CONTEXT.customer_id, cmd.robot_binding_id,
                                               cmd.dispatch_generation, cmd.command_id, terminal_state))

    await finish_step(first.task_id, "move_to", "failed" if cancel_first else "succeeded")
    if not cancel_first:
        await finish_step(first.task_id, "vla_skill", "succeeded")
    first_final = await service.get(CONTEXT, first.task_id)
    assert first_final.state == ("CANCELLED" if cancel_first else "SUCCEEDED")
    assert (await service.get(CONTEXT, second.task_id)).result_summary == ()
    await finish_step(second.task_id, "move_to", "succeeded")
    await finish_step(second.task_id, "vla_skill", "succeeded")
    await service.dispatch_next("test-deployment")
    second_final = await service.get(CONTEXT, second.task_id)
    assert second_final.state == "SUCCEEDED"
    expected = [(first.task_id, "move_to")]
    if not cancel_first:
        expected.append((first.task_id, "vla_skill"))
    expected += [(second.task_id, "move_to"), (second.task_id, "vla_skill")]
    assert [(cmd.task_id, cmd.wire_skill) for cmd in robot.sent] == expected
