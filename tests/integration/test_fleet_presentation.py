# ruff: noqa: F811 -- imported pytest fixture
from dataclasses import replace
from unittest.mock import AsyncMock
import pytest
from robot_agent.domain.models import DependencyUnavailable, PlanError
from tests.integration.test_scheduling import fleet, USER, ADMIN, INPUT  # noqa: F401
from tests.fixtures.submission import submit_and_plan

pytestmark = pytest.mark.integration


async def test_list_includes_offline_disabled_and_keeps_customer_scope(fleet, postgres_store):
    _, _, resources, _, _, _, connect = fleet
    await connect("A")
    await postgres_store.pool.execute(
        "UPDATE ra_robot_bindings SET enabled=false WHERE binding_id='binding-b'"
    )
    rows = await resources.list_robots(USER)
    assert {r.binding_id for r in rows} == {"test-binding", "binding-b"}
    assert next(r for r in rows if r.binding_id == "test-binding").connected
    b = await resources.robot_detail(USER, "binding-b")
    assert not b.robot.enabled and b.snapshot is None
    with pytest.raises(PermissionError):
        await resources.robot_detail(USER, "binding-c")
    assert await resources.list_robots(replace(USER, customer_id="empty")) == []


async def test_bad_new_report_never_overwrites_or_refreshes_good_data(fleet, postgres_store):
    _, robot, resources, _, sessions, _, connect = fleet
    await connect("A")
    await connect("B")
    before = await postgres_store.pool.fetchval(
        "SELECT observations FROM ra_robot_bindings WHERE binding_id='binding-b'"
    )
    with pytest.raises(PlanError):
        await robot.handle(sessions["B"], {"type": "readiness", "brain_skills": 7, "nav_server_ready": True})
    assert (
        await postgres_store.pool.fetchval(
            "SELECT observations FROM ra_robot_bindings WHERE binding_id='binding-b'"
        )
        == before
    )
    assert (await resources.snapshot(USER, "test-binding")).brain_skills


@pytest.mark.parametrize(
    "path,value",
    [
        ("{brain_skills,value}", "7"),
        ("{nav_server_ready,value}", '"yes"'),
        ("{nav_server_ready,received_at}", '"bad-time"'),
        ("{busy,connection_generation}", '"wrong"'),
    ],
)
async def test_old_bad_report_only_excludes_that_robot(fleet, postgres_store, path, value):
    service, _, resources, _, _, frames, connect = fleet
    await connect("A")
    await connect("B")
    await postgres_store.pool.execute(
        "UPDATE ra_robot_bindings SET observations=jsonb_set(observations,$1::text[],$2::jsonb) WHERE binding_id='binding-b'",
        path.strip("{}").split(","),
        value,
    )
    details = await resources.online_robot_details(USER)
    assert [s.robot_binding_id for s in details] == ["test-binding"]
    bad = await resources.robot_detail(USER, "binding-b")
    assert "REPORT_INVALID" in bad.robot.issues and bad.snapshot is None
    with pytest.raises(DependencyUnavailable, match="REPORT_INVALID"):
        await resources.snapshot(USER, "binding-b")
    task = await submit_and_plan(service, USER, INPUT, "bad-B")
    assert task.plan_robot_binding_id == "test-binding"
    assert not any(frames.values())


async def test_null_is_unknown_and_offline_reports_are_history(fleet):
    _, robot, resources, _, sessions, _, connect = fleet
    await connect("A")
    await robot.handle(sessions["A"], {"type": "readiness", "nav_server_ready": None})
    detail = await resources.robot_detail(USER, "test-binding")
    assert "nav_server_ready" not in detail.snapshot.readiness
    await robot.unregister(sessions["A"])
    detail = await resources.robot_detail(USER, "test-binding")
    assert not detail.robot.connected and detail.snapshot is None
    assert detail.last_report["waypoint_names"]["value"] == ["test-table"]


@pytest.mark.parametrize(
    "error", [RuntimeError("database offline"), TypeError("program bug"), ValueError("program bug")]
)
async def test_database_and_program_errors_are_not_swallowed(fleet, monkeypatch, error):
    _, _, resources, _, _, _, connect = fleet
    await connect("A")
    monkeypatch.setattr(resources, "_snapshot", AsyncMock(side_effect=error))
    with pytest.raises(type(error), match=str(error)):
        await resources.online_robot_details(USER)
    with pytest.raises(type(error), match=str(error)):
        await resources.robot_detail(USER, "test-binding")


async def test_credential_and_bad_data_reasons_coexist_and_repair_is_narrow(fleet, postgres_store):
    _, robot, resources, _, sessions, _, connect = fleet
    await connect("B")
    resources.missing_credentials = frozenset({(USER.customer_id, "binding-b")})
    await postgres_store.pool.execute(
        "UPDATE ra_robot_bindings SET isolation_reason='EVIDENCE_CONFLICT',enabled=false,observations=jsonb_set(observations,'{brain_skills,value}','7') WHERE binding_id='binding-b'"
    )
    before = await resources.robot_detail(USER, "binding-b")
    assert {"CREDENTIAL_MISSING", "REPORT_INVALID", "DISABLED", "ISOLATED"} <= set(before.robot.issues)
    resources.missing_credentials = frozenset()
    after = await resources.robot_detail(USER, "binding-b")
    assert "CREDENTIAL_MISSING" not in after.robot.issues
    assert {"REPORT_INVALID", "DISABLED", "ISOLATED"} <= set(after.robot.issues)
    assert after.snapshot is None


async def test_resource_read_and_write_target_exact_robot(fleet):
    _, _, resources, _, _, _, _ = fleet
    await resources.put(
        ADMIN, "test-binding", "environment", {"other": "A新地点"}, ("test:source",), base_version=1
    )
    assert (await resources.read(ADMIN, "environment", "binding-b"))["entries"] == {"test-table": "桌边 B"}
    assert (await resources.read(ADMIN, "environment", "test-binding"))["revision"] == 2
    with pytest.raises(PermissionError):
        await resources.read(USER, "environment", "binding-c")
    with pytest.raises(DependencyUnavailable):
        await resources.read(USER, "environment")


async def test_task_allocation_plan_and_command_have_distinct_lifetimes(fleet):
    service, _, _, _, _, _, connect = fleet
    await connect("A")
    service.policy.result = "require_confirmation"
    view = await submit_and_plan(service, USER, INPUT, "waiting")
    assert view.allocation_robot_binding_id is None
    assert view.plan_robot_binding_id == "test-binding"
    approved = await service.decide(
        ADMIN, view.task_id, view.version, "approve", "approve", plan_id=view.plan_id
    )
    assert approved.allocation_robot_binding_id == "test-binding"
    await service.dispatch_next("test-deployment")
    running = await service.get(USER, view.task_id)
    assert running.result_summary[0]["robot_binding_id"] == "test-binding"
    await connect("B")
    history = await service.history(USER, limit=10, before=None)
    assert history["tasks"][0]["plan_robot_binding_id"] == "test-binding"
    assert history["tasks"][0]["allocation_robot_binding_id"] == "test-binding"


async def test_actual_robot_catalog_is_grounded_in_registered_capability(fleet):
    _, robot, resources, _, sessions, _, connect = fleet
    await connect("A")
    await robot.handle(
        sessions["A"],
        {
            "type": "readiness",
            "brain_skills": [
                {"id": "test-pick", "name": "拿水", "description": "拿瓶水", "status": "ready"},
                {"id": "unregistered", "name": "未登记", "description": "不可采用", "status": "ready"},
            ],
        },
    )
    assert set((await resources.snapshot(USER, "test-binding")).brain_skills) == {"test-pick"}


async def test_clarification_and_cancel_display_no_current_allocation(fleet):
    service, _, _, _, _, _, connect = fleet
    await connect("A")
    service.planner.value = {"outcome": "clarify", "question": "去哪？", "required_slots": ["location"]}
    view = await submit_and_plan(service, USER, INPUT, "clarify")
    assert view.state == "NEEDS_CLARIFICATION"
    assert view.allocation_robot_binding_id is None and view.plan_robot_binding_id is None
    cancelled = await service.cancel(USER, view.task_id, view.version, "cancel-question")
    assert cancelled.state == "CANCELLED" and cancelled.allocation_robot_binding_id is None


async def test_queued_task_is_not_shown_as_allocated(fleet):
    service, _, _, _, _, _, connect = fleet
    await connect("A")
    first = await submit_and_plan(service, USER, INPUT, "first")
    second = await submit_and_plan(service, USER, INPUT, "second")
    assert first.allocation_robot_binding_id == "test-binding"
    assert second.state == "QUEUED"
    assert second.allocation_robot_binding_id is None and second.plan_robot_binding_id is None
