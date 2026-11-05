"""Upgrade a populated v7 schema, not just an empty database."""
from tests.fixtures.submission import submit_and_plan
from dataclasses import replace
from hashlib import sha256
import json
import os
from pathlib import Path
from uuid import uuid4

import asyncpg
import pytest

from robot_agent.adapters.postgres import PostgresStore
from robot_agent.domain.models import ChangeSet, TaskInput, TrustedContext
from tests.fixtures.minimal import build_service


pytestmark = pytest.mark.integration
USER = TrustedContext('test-customer', 'user', 'test-user')


async def test_populated_v7_upgrade_preserves_history_and_allows_preplan_allocation(postgres_store):
    # The fixture already checks the DSN is local and ends in _test.
    schema = 'ra_upgrade_' + uuid4().hex
    await postgres_store.pool.execute(f'CREATE SCHEMA "{schema}"')
    pool = await asyncpg.create_pool(os.environ['RO_AGENT_TEST_DSN'], min_size=1, max_size=3,
                                    server_settings={'search_path':schema})
    store = PostgresStore(pool, deployment_id='test-deployment')
    try:
        await pool.execute('CREATE TABLE ra_schema_migrations(version integer PRIMARY KEY, checksum text NOT NULL)')
        for path in sorted((Path(__file__).parents[2] / 'db/migrations').glob('*.sql')):
            version = int(path.name.split('_')[0])
            if version >= 8:
                break
            sql = path.read_text(encoding='utf-8')
            await pool.execute(sql)
            await pool.execute('INSERT INTO ra_schema_migrations VALUES($1,$2)', version, sha256(sql.encode()).hexdigest())
        await pool.execute("""INSERT INTO ra_robot_bindings(customer_id,binding_id,wire_robot_id,enabled,
            allowed_capabilities,connection_generation) VALUES('test-customer','test-binding','test-robot',true,
            ARRAY['move_to','vla_skill'],1)""")
        service, _, memory = build_service()
        view = await submit_and_plan(service, USER, TaskInput('legacy','web','去桌边',''), 'legacy')
        await service.dispatch_next('test-deployment')
        old = memory.tasks[view.task_id]
        async with store.unit_of_work(USER) as tx:
            await tx.insert_task(replace(old, version=1, plan=None, snapshot=None, commands=()))
            await tx.apply_changes(ChangeSet(replace(old, version=2)))
        await pool.execute("""INSERT INTO ra_execution_guard(deployment_id,customer_id,task_id,unit_id,binding_id,state)
            VALUES('test-deployment',$1,$2,$3,$4,'ACTIVE')""", old.customer_id, old.task_id, old.unit_id, old.snapshot.robot_binding_id)
        tables = ['ra_tasks','ra_plans','ra_units','ra_commands']
        before = {table: await pool.fetch(f'SELECT * FROM {table}') for table in tables}
        await store.migrate()
        await store.migrate()  # Re-running the startup upgrade is harmless.
        assert await pool.fetchval('SELECT count(*) FROM ra_schema_migrations') == 9
        for table in tables:
            assert await pool.fetch(f'SELECT * FROM {table}') == before[table]
        async with store.system_unit_of_work() as tx:
            allocation = await tx.task_allocation(old)
            assert allocation.task_id == old.task_id and allocation.snapshot == old.snapshot
            await tx.release_allocation(allocation)
        fresh = replace(old, task_id='fresh', unit_id='fresh-unit', version=1, state='RECEIVED',
                        plan=None, snapshot=None, commands=())
        async with store.unit_of_work(USER) as tx:
            await tx.insert_task(fresh)
        async with store.system_unit_of_work() as tx:
            await tx.load_task(fresh.task_id)
            await tx.claim_slot('test-deployment', fresh, old.snapshot)
            await tx.apply_changes(ChangeSet(replace(fresh, version=2, state='PLANNING')))
        assert await pool.fetchval("SELECT count(*) FROM ra_units WHERE task_id='fresh'") == 0
        assert json.loads(await pool.fetchval('SELECT allocation_snapshot FROM ra_execution_guard'))['snapshot_id'] == old.snapshot.snapshot_id
        await pool.execute("""INSERT INTO ra_robot_bindings(customer_id,binding_id,wire_robot_id,enabled)
            VALUES('other-customer','foreign-binding','foreign-robot',true)""")
        with pytest.raises(asyncpg.ForeignKeyViolationError):
            await pool.execute("UPDATE ra_execution_guard SET binding_id='foreign-binding'")
        assert await pool.fetchval('SELECT binding_id FROM ra_execution_guard') == 'test-binding'
    finally:
        await pool.close()
        await postgres_store.pool.execute(f'DROP SCHEMA "{schema}" CASCADE')


@pytest.mark.parametrize('kind', ['preplan', 'unknown', 'duplicate_robot'])
async def test_v8_upgrade_preserves_active_evidence_or_rolls_back_atomically(postgres_store, kind):
    from dataclasses import asdict
    from robot_agent.adapters.postgres import _json
    schema = 'ra_upgrade_' + uuid4().hex
    await postgres_store.pool.execute(f'CREATE SCHEMA "{schema}"')
    pool = await asyncpg.create_pool(os.environ['RO_AGENT_TEST_DSN'], min_size=1, max_size=3,
                                    server_settings={'search_path': schema})
    store = PostgresStore(pool, deployment_id='test-deployment')
    try:
        await pool.execute('CREATE TABLE ra_schema_migrations(version integer PRIMARY KEY, checksum text NOT NULL)')
        for path in sorted((Path(__file__).parents[2] / 'db/migrations').glob('*.sql')):
            version = int(path.name.split('_')[0])
            if version >= 9:
                break
            sql = path.read_text(encoding='utf-8')
            await pool.execute(sql)
            await pool.execute('INSERT INTO ra_schema_migrations VALUES($1,$2)', version, sha256(sql.encode()).hexdigest())
        await pool.execute("""INSERT INTO ra_robot_bindings(customer_id,binding_id,wire_robot_id,enabled,
            allowed_capabilities,connection_generation) VALUES('test-customer','test-binding','test-robot',true,
            ARRAY['move_to','vla_skill'],1)""")
        service, _, memory = build_service()
        view = await submit_and_plan(service, USER, TaskInput('legacy','web','go',''), 'legacy')
        await service.dispatch_next('test-deployment')
        old = memory.tasks[view.task_id]
        if kind == 'preplan':
            stored = replace(old, state='PLANNING', plan=None, snapshot=None, commands=(), version=1)
        else:
            from robot_agent.domain.transitions import uncertain_execution
            stored = replace(uncertain_execution(old).task, version=2)
        async with store.unit_of_work(USER) as tx:
            await tx.insert_task(replace(stored, version=1, plan=None, snapshot=None, commands=()))
            if stored.plan:
                await tx.apply_changes(ChangeSet(stored))
        sql = """INSERT INTO ra_execution_guard(deployment_id,customer_id,task_id,unit_id,binding_id,state,allocation_snapshot)
                 VALUES($1,$2,$3,$4,$5,'ACTIVE',$6::jsonb)"""
        values = [old.customer_id, old.task_id, old.unit_id, old.snapshot.robot_binding_id, _json(asdict(old.snapshot))]
        await pool.execute(sql, 'test-deployment', *values)
        if kind == 'duplicate_robot':
            await pool.execute(sql, 'another-deployment', *values)
        tables = ['ra_tasks', 'ra_plans', 'ra_units', 'ra_commands', 'ra_decisions', 'ra_task_events']
        before = {table: await pool.fetch(f'SELECT * FROM {table}') for table in tables}
        guards = await pool.fetch('SELECT * FROM ra_execution_guard ORDER BY deployment_id')
        if kind == 'duplicate_robot':
            with pytest.raises(asyncpg.UniqueViolationError):
                await store.migrate()
            assert await pool.fetchval('SELECT max(version) FROM ra_schema_migrations') == 8
            assert await pool.fetch('SELECT * FROM ra_execution_guard ORDER BY deployment_id') == guards
        else:
            await store.migrate()
            async with store.system_unit_of_work() as tx:
                allocation = await tx.task_allocation(stored)
                assert allocation.task_id == old.task_id and allocation.snapshot == old.snapshot and allocation.allocation_id
            first_id = allocation.allocation_id
            await store.migrate()
            assert await pool.fetchval('SELECT allocation_id FROM ra_execution_guard') == first_id
        for table in tables:
            assert await pool.fetch(f'SELECT * FROM {table}') == before[table]
    finally:
        await pool.close()
        await postgres_store.pool.execute(f'DROP SCHEMA "{schema}" CASCADE')
