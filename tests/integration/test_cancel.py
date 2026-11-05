from tests.fixtures.submission import submit_and_plan
import pytest

from robot_agent.domain.models import RobotEvent, TaskInput, TrustedContext


pytestmark = pytest.mark.integration
CONTEXT = TrustedContext("test-customer", "user", "test-user")


@pytest.mark.parametrize("terminal_state, outcome", [("succeeded", "SUCCEEDED"), ("failed", "FAILED")])
async def test_cancel_waits_for_robot_terminal_status(persisted_service, terminal_state, outcome):
    service, robot = persisted_service
    view = await submit_and_plan(service, CONTEXT, TaskInput("req", "web", "test", ""), "key")
    await service.dispatch_next("test-deployment")
    current = await service.get(CONTEXT, view.task_id)
    cancelled = await service.cancel(CONTEXT, view.task_id, current.version, "cancel-key")
    assert cancelled.state == "CANCEL_REQUESTED"
    assert cancelled.result_summary[0]["outcome"] == "NONE"
    await service.on_robot_event(RobotEvent("late", CONTEXT.customer_id, "test-binding", 1,
                                          robot.sent[0].command_id, terminal_state))
    final = await service.get(CONTEXT, view.task_id)
    assert final.state == "CANCELLED"
    assert final.result_summary[0]["outcome"] == outcome
    assert not final.reason_code
    await service.dispatch_next("test-deployment")
    assert len(robot.sent) == 1


async def test_queue_cancel_sends_no_robot_cancel(persisted_service):
    service, robot = persisted_service
    async def forbidden_cancel(command):
        raise AssertionError("unstarted queue task cannot send cancel")
    robot.cancel = forbidden_cancel
    view = await submit_and_plan(service, CONTEXT, TaskInput("req", "web", "test", ""), "key")
    final = await service.cancel(CONTEXT, view.task_id, view.version, "cancel-key")
    assert final.state == "CANCELLED"
    await service.dispatch_next("test-deployment")
    assert robot.sent == []


async def test_dlv_cancel_write_uses_existing_wire_frame(persisted_service):
    from tests.integration.test_robot_ws import connect_session, TestWriteGate
    from robot_agent.adapters.dlv0 import Dlv0Robot
    service, _ = persisted_service
    robot = Dlv0Robot(service=service, write_gate=TestWriteGate(), max_frame_bytes=8192)
    service.robot = robot
    session, frames = await connect_session(robot)
    view = await submit_and_plan(service, CONTEXT, TaskInput("req", "web", "test", ""), "key")
    await service.dispatch_next("test-deployment")
    current = await service.get(CONTEXT, view.task_id)
    final = await service.cancel(CONTEXT, view.task_id, current.version, "cancel-key")
    assert final.state == "CANCEL_REQUESTED"
    assert frames[-1] == {"type": "cancel", "cmd_id": frames[0]["cmd_id"]}
