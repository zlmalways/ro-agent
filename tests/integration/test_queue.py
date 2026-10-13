import pytest

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
