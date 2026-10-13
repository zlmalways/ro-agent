"""Regressions from the one independent review; real PG and production adapters."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json

import httpx
import pytest

from robot_agent.adapters.dlv0 import Dlv0Robot
from robot_agent.adapters.identity import ConfiguredIdentity
from robot_agent.adapters.policy import ConfiguredPolicy
from robot_agent.application.resources import Resources
from robot_agent.app import create_app
from robot_agent.application.input_service import InputService
from robot_agent.domain.models import DependencyUnavailable, TaskInput, TrustedContext
from tests.integration.test_robot_ws import TestWriteGate as WriteFixture, connect_session


pytestmark = pytest.mark.integration
USER = TrustedContext("test-customer", "user", "test-user")
INPUT = TaskInput("req", "web", "test", "")


def configured_policy(service):
    identity = ConfiguredIdentity(users={
        "test-user-token": {"customer_id": "test-customer", "actor_id": "test-user", "roles": ["user"]},
        "test-approver-token": {"customer_id": "test-customer", "actor_id": "test-approver", "roles": ["approver"]},
        "test-stranger-token": {"customer_id": "test-customer", "actor_id": "test-stranger", "roles": ["user"]},
    }, robots={})
    service.policy = ConfiguredPolicy(identity, version="test-policy:v1",
                                     actions={"test-customer": {"move_to": "require_confirmation", "vla_skill": "require_confirmation"}},
                                     approval_ttl_seconds=300)
    service.preflight.policy = service.policy
    return identity


async def test_cancel_intent_survives_disconnect_and_replayed_success(persisted_service):
    service, _ = persisted_service
    robot = Dlv0Robot(service=service, write_gate=WriteFixture(), max_frame_bytes=8192)
    service.robot = robot
    session, frames = await connect_session(robot)
    view = await service.submit(USER, INPUT, "key")
    await service.dispatch_next("test-deployment")
    current = await service.get(USER, view.task_id)
    await service.cancel(USER, view.task_id, current.version, "cancel")
    command_id = frames[0]["cmd_id"]
    await robot.unregister(session)
    await service.recover("test-deployment")
    new_session, new_frames = await connect_session(robot, busy_cmd_id=command_id)
    await robot.handle(new_session, {"type": "cmd_status", "cmd_id": command_id, "state": "succeeded"})
    await service.dispatch_next("test-deployment")
    result = await service.get(USER, view.task_id)
    assert result.state == "CANCELLED"
    assert result.result_summary[0]["outcome"] == "SUCCEEDED"
    assert new_frames == []


async def test_formal_cancel_policy_works_and_rejects_stranger(persisted_service):
    service, _ = persisted_service
    identity = configured_policy(service)
    view = await service.submit(USER, INPUT, "key")
    app = create_app(service=service, identity=identity, inputs=InputService(service, None, max_audio_bytes=1024))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
        result = await client.post(f"/api/robot-agent/tasks/{view.task_id}/cancel", json={"expected_version": view.version},
                                   headers={"Authorization": "Bearer test-user-token", "Idempotency-Key": "cancel"})
        assert result.status_code == 200
        assert result.json()["state"] == "CANCELLED"


async def test_independent_approver_can_read_and_approve_only_same_customer(persisted_service):
    service, _ = persisted_service
    identity = configured_policy(service)
    view = await service.submit(USER, INPUT, "key")
    app = create_app(service=service, identity=identity, inputs=InputService(service, None, max_audio_bytes=1024))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
        path = f"/api/robot-agent/tasks/{view.task_id}"
        assert (await client.get(path, headers={"Authorization": "Bearer test-stranger-token"})).status_code == 404
        assert (await client.get(path, headers={"Authorization": "Bearer test-approver-token"})).status_code == 200
        result = await client.post(path + "/decisions", json={"plan_id": view.plan_id, "decision": "approve", "expected_version": view.version},
                                   headers={"Authorization": "Bearer test-approver-token", "Idempotency-Key": "approve"})
        assert result.status_code == 202
        assert result.json()["state"] == "READY"


async def test_prepared_approval_resumes_same_command_once(persisted_service, postgres_store):
    service, robot = persisted_service
    view = await service.submit(USER, INPUT, "key")
    calls = 0

    async def policy_changes(context, plan, approvals):
        nonlocal calls
        calls += 1
        return "allow" if calls == 1 else "require_confirmation"

    service.policy.evaluate = policy_changes
    await service.dispatch_next("test-deployment")
    current = await service.get(USER, view.task_id)
    command_id = current.result_summary[0]["command_id"]
    assert current.result_summary[0]["dispatch_state"] == "PREPARED"
    await service.decide(USER, view.task_id, current.version, "approve", "approval-key")
    await service.dispatch_next("test-deployment")
    assert len(robot.sent) == 1
    assert robot.sent[0].command_id == command_id
    assert await postgres_store.pool.fetchval("SELECT count(*) FROM ra_commands") == 1


async def formal_resources(store):
    resources = Resources(store, freshness_seconds=60, max_entries=100)
    admin = replace(USER, roles=frozenset({"maintainer"}))
    await resources.put(admin, "test-binding", "environment", {"test-table": "verified test table"}, ("test-survey",))
    await resources.put(admin, "test-binding", "capability", {"test-pick": "verified test pick"}, ("test-preset",))
    return resources


async def connected_formal_service(service, store):
    service.resources = await formal_resources(store)
    robot = Dlv0Robot(service=service, write_gate=WriteFixture(), max_frame_bytes=8192)
    service.robot = robot
    session, frames = await connect_session(robot)
    await robot.handle(session, {"type": "readiness", "nav_server_ready": True, "brain_server_ready": True,
                                 "brain_engine": "ready", "waypoint_names": ["test-table"], "brain_skills": [{"skill_id": "test-pick"}]})
    return robot, session, frames


async def test_latest_readiness_invalidates_physical_write(persisted_service, postgres_store):
    service, _ = persisted_service
    robot, session, frames = await connected_formal_service(service, postgres_store)
    await service.submit(USER, INPUT, "key")
    original_send = robot.send

    async def change_before_send(command):
        await robot.handle(session, {"type": "readiness", "nav_server_ready": False})
        return await original_send(command)

    robot.send = change_before_send
    await service.dispatch_next("test-deployment")
    assert frames == []


async def test_latest_approval_revocation_invalidates_physical_write(persisted_service, postgres_store):
    service, _ = persisted_service
    robot, session, frames = await connected_formal_service(service, postgres_store)
    service.policy.result = "require_confirmation"
    view = await service.submit(USER, INPUT, "key")
    await service.decide(USER, view.task_id, view.version, "approve", "approve")
    original_send = robot.send

    async def revoke_before_send(command):
        await postgres_store.pool.execute("UPDATE ra_decisions SET revoked_at=clock_timestamp() WHERE kind='approval'")
        return await original_send(command)

    robot.send = revoke_before_send
    await service.dispatch_next("test-deployment")
    assert frames == []


async def test_resource_version_change_during_write_check_invalidates_send(persisted_service, postgres_store):
    service, _ = persisted_service
    robot, session, frames = await connected_formal_service(service, postgres_store)
    await service.submit(USER, INPUT, "key")
    original = service.preflight.check
    async def change_after_validation(context, plan, snapshot, phase="admission", approvals=()):
        result = await original(context, plan, snapshot, phase, approvals)
        if phase == "write":
            await service.resources.put(replace(USER, roles=frozenset({"maintainer"})), "test-binding", "environment",
                                        {"test-table": "changed mapping"}, ("test-survey:changed",))
        return result
    service.preflight.check = change_after_validation
    await service.dispatch_next("test-deployment")
    assert frames == []


async def test_reconnect_during_prepared_check_requires_current_evidence(persisted_service, postgres_store):
    service, _ = persisted_service
    robot, session, frames = await connected_formal_service(service, postgres_store)
    await service.submit(USER, INPUT, "key")
    original = service.preflight.check
    new_frames = []
    calls = 0
    async def reconnect_after_preflight(context, plan, snapshot, phase="admission", approvals=()):
        nonlocal calls, new_frames
        result = await original(context, plan, snapshot, phase, approvals)
        calls += 1
        if calls == 2:
            _, new_frames = await connect_session(robot)
        return result
    service.preflight.check = reconnect_after_preflight
    await service.dispatch_next("test-deployment")
    command = await postgres_store.pool.fetchrow("SELECT dispatch_state FROM ra_commands")
    assert command["dispatch_state"] == "PREPARED"
    assert frames == new_frames == []


async def test_hello_skills_remain_valid_in_current_connection(persisted_service, postgres_store):
    service, _ = persisted_service
    robot, session, frames = await connected_formal_service(service, postgres_store)
    observations = json.loads(await postgres_store.pool.fetchval("SELECT observations FROM ra_robot_bindings"))
    observations["skills"]["received_at"] = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    await postgres_store.pool.execute("UPDATE ra_robot_bindings SET observations=$1::jsonb", json.dumps(observations))
    snapshot = await service.resources.snapshot(USER)
    assert snapshot.skills == frozenset({"move_to", "vla_skill"})


async def test_command_deadline_marks_unknown_and_keeps_slot(persisted_service, postgres_store):
    service, robot = persisted_service
    service.command_timeout_seconds = 30
    view = await service.submit(USER, INPUT, "key")
    await service.dispatch_next("test-deployment")
    await postgres_store.pool.execute("UPDATE ra_commands SET dispatch_started_at=clock_timestamp()-interval '60 seconds'")
    await service.expire_commands("test-deployment")
    result = await service.get(USER, view.task_id)
    assert result.reason_code == "RESULT_UNKNOWN"
    assert result.result_summary[0]["outcome"] == "RESULT_UNKNOWN"
    assert await postgres_store.pool.fetchval("SELECT task_id FROM ra_execution_guard") == view.task_id
    await service.dispatch_next("test-deployment")
    assert len(robot.sent) == 1


async def test_snapshot_failure_records_blocked_phase_and_same_key_can_retry(persisted_service, postgres_store):
    service, robot = persisted_service
    original = service.resources.snapshot

    async def unavailable(context):
        raise DependencyUnavailable("ROBOT_BINDING_UNAVAILABLE")

    service.resources.snapshot = unavailable
    with pytest.raises(DependencyUnavailable):
        await service.submit(USER, INPUT, "same-key")
    record = await postgres_store.pool.fetchrow("SELECT task_id,state,reason_code,resume_phase FROM ra_tasks")
    assert record["state"] == "BLOCKED"
    assert record["reason_code"] == "ROBOT_BINDING_UNAVAILABLE"
    assert record["resume_phase"] == "PLANNING"
    service.resources.snapshot = original
    result = await service.submit(USER, INPUT, "same-key")
    assert result.task_id == record["task_id"]
    assert result.state == "READY"
    assert await postgres_store.pool.fetchval("SELECT count(*) FROM ra_tasks") == 1
    assert robot.sent == []
