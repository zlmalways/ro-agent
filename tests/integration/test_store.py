"""Storage isolation and deduplication, against a real PostgreSQL server."""

import asyncio
from dataclasses import replace

import asyncpg
import pytest

from robot_agent.domain.models import ConflictError, NotFoundError, Task, TaskInput, TrustedContext


pytestmark = pytest.mark.integration
CONTEXT = TrustedContext("test-customer", "user", "test-user")
INPUT = TaskInput("req-1", "web", "test: fetch cup", "")


async def test_request_key_idempotency(persisted_service, postgres_store):
    service, robot = persisted_service
    first = await service.submit(CONTEXT, INPUT, "same-key")
    repeated = await service.submit(CONTEXT, replace(INPUT, request_id="req-2"), "same-key")
    assert repeated.task_id == first.task_id
    assert repeated.plan_id == first.plan_id
    assert await postgres_store.pool.fetchval("SELECT count(*) FROM ra_tasks") == 1
    with pytest.raises(ConflictError):
        await service.submit(CONTEXT, replace(INPUT, text="other request"), "same-key")
    assert robot.sent == []


async def test_request_digest_includes_channel(persisted_service):
    service, _ = persisted_service
    await service.submit(CONTEXT, INPUT, "key")
    with pytest.raises(ConflictError):
        await service.submit(CONTEXT, replace(INPUT, channel="robot"), "key")


async def test_concurrent_request_key_creates_one_task(persisted_service, postgres_store):
    service, _ = persisted_service
    first, second = await asyncio.gather(service.submit(CONTEXT, INPUT, "key"),
                                        service.submit(CONTEXT, INPUT, "key"))
    assert first.task_id == second.task_id
    assert await postgres_store.pool.fetchval("SELECT count(*) FROM ra_tasks") == 1
    assert await postgres_store.pool.fetchval("SELECT count(*) FROM ra_request_keys") == 1


async def test_cross_customer_fk_rejected(postgres_store):
    task = Task("task-a", "customer-a", "user", "user-a", "test", unit_id="unit-a")
    async with postgres_store.unit_of_work(TrustedContext("customer-a", "user", "user-a")) as tx:
        await tx.insert_task(task)
    with pytest.raises(asyncpg.ForeignKeyViolationError):
        await postgres_store.pool.execute("""
            INSERT INTO ra_plans (customer_id, plan_id, task_id, revision, steps, snapshot)
            VALUES ('customer-b', 'plan-b', 'task-a', 1, '[]', '{}')
        """)
    with pytest.raises(asyncpg.ForeignKeyViolationError):
        await postgres_store.pool.execute("""
            INSERT INTO ra_request_keys (customer_id, actor_id, operation, key, request_digest, result_ref)
            VALUES ('customer-b', 'user-b', 'submit', 'key', 'digest', 'task-a')
        """)


async def test_store_hides_other_customer_and_actor(persisted_service, postgres_store):
    service, _ = persisted_service
    view = await service.submit(CONTEXT, INPUT, "key")
    for stranger in (TrustedContext("other", "user", "test-user"),
                     TrustedContext("test-customer", "user", "other-user")):
        async with postgres_store.unit_of_work(stranger) as tx:
            with pytest.raises(NotFoundError):
                await tx.load_task(view.task_id)


async def test_version_condition_rejects_stale_update(persisted_service, postgres_store):
    service, _ = persisted_service
    view = await service.submit(CONTEXT, INPUT, "key")
    async with postgres_store.unit_of_work(CONTEXT) as tx:
        original = await tx.load_task(view.task_id)
    async with postgres_store.unit_of_work(CONTEXT) as tx:
        await tx.save_task(replace(original, reason_code="first", version=original.version + 1),
                           expected_version=original.version)
    async with postgres_store.unit_of_work(CONTEXT) as tx:
        with pytest.raises(ConflictError):
            await tx.save_task(replace(original, reason_code="stale", version=original.version + 1),
                               expected_version=original.version)
    assert (await service.get(CONTEXT, view.task_id)).reason_code == "first"


async def test_plan_version_cannot_be_overwritten(persisted_service, postgres_store):
    service, _ = persisted_service
    view = await service.submit(CONTEXT, INPUT, "key")
    async with postgres_store.unit_of_work(CONTEXT) as tx:
        task = await tx.load_task(view.task_id)
        changed_step = replace(task.plan.steps[0], args={"location": "different-target"})
        mutated = replace(task, plan=replace(task.plan, steps=(changed_step,) + task.plan.steps[1:]),
                          version=task.version + 1)
        with pytest.raises(ConflictError):
            await tx.save_task(mutated, expected_version=task.version)
    async with postgres_store.unit_of_work(CONTEXT) as tx:
        assert (await tx.load_task(view.task_id)).plan.steps[0].args == {"location": "test-table"}


async def test_adoption_persists_plan_decisions_and_event(persisted_service, postgres_store):
    service, _ = persisted_service
    view = await service.submit(CONTEXT, INPUT, "key")
    rows = await postgres_store.pool.fetch("SELECT kind, result FROM ra_decisions ORDER BY kind")
    assert {(row["kind"], row["result"]) for row in rows} == {("validation", "valid"), ("authorization", "allow")}
    events = await postgres_store.pool.fetch("SELECT seq, task_version FROM ra_task_events ORDER BY seq")
    assert [row["seq"] for row in events] == [1, 2]
    assert events[-1]["task_version"] == view.version
