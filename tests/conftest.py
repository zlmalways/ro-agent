"""Every test uses its own schema inside an explicitly named test database."""

import os
from urllib.parse import urlsplit
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio

from robot_agent.adapters.postgres import PostgresStore
from tests.fixtures.minimal import build_service


@pytest_asyncio.fixture
async def postgres_store():
    dsn = os.environ.get("RO_AGENT_TEST_DSN")
    if not dsn:
        pytest.skip("RO_AGENT_TEST_DSN is required for real PostgreSQL integration tests")
    location = urlsplit(dsn)
    if not location.path.endswith("_test") or location.hostname not in {"127.0.0.1", "localhost"}:
        pytest.fail("Integration tests require a local, explicitly named *_test database")
    schema = "ra_test_" + uuid4().hex
    admin = await asyncpg.connect(dsn)
    await admin.execute(f'CREATE SCHEMA "{schema}"')
    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=8,
                                    server_settings={"search_path": schema, "application_name": schema})
    store = PostgresStore(pool, deployment_id="test-deployment")
    try:
        await store.migrate()
        await pool.execute("""INSERT INTO ra_robot_bindings
            (customer_id,binding_id,wire_robot_id,enabled,allowed_capabilities,connection_generation)
            VALUES ('test-customer','test-binding','test-robot',true,ARRAY['move_to','vla_skill'],1)""")
        yield store
    finally:
        await pool.close()
        await admin.execute(f'DROP SCHEMA "{schema}" CASCADE')
        await admin.close()


@pytest.fixture
def persisted_service(postgres_store):
    service, robot, _ = build_service()
    service.store = postgres_store
    return service, robot
