"""Linux + actual child process + real PostgreSQL; never a boolean-lock test."""

import asyncio
import json
import os
from pathlib import Path
import sys

import pytest

from robot_agent.runtime import RuntimeOwnership


pytestmark = [pytest.mark.integration, pytest.mark.skipif(sys.platform != "linux", reason="real flock needs Linux")]


@pytest.fixture
async def owner_options(postgres_store, tmp_path):
    return {"dsn": os.environ["RO_AGENT_TEST_DSN"], "deployment_id": "test-deployment", "host_id": "test-host",
            "lock_path": str(tmp_path / "deployment.lock"), "advisory_key": 490013,
            "schema": await postgres_store.pool.fetchval("SELECT current_schema()")}


async def test_duplicate_owner_cannot_send(owner_options):
    owner = RuntimeOwnership(**owner_options)
    assert await owner.acquire()
    code = """
import asyncio,json,sys
from robot_agent.runtime import RuntimeOwnership
from robot_agent.adapters.dlv0 import Dlv0Robot
async def run():
    owner=RuntimeOwnership(**json.loads(sys.stdin.read()))
    acquired=await owner.acquire()
    robot=Dlv0Robot(service=None,write_gate=owner,max_frame_bytes=8192)
    result=await robot.send(None)
    print(acquired,await owner.healthy(),result.sent,flush=True)
    await owner.close()
asyncio.run(run())
"""
    child = await asyncio.create_subprocess_exec(sys.executable, "-c", code, stdin=asyncio.subprocess.PIPE,
                                                  stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        output, errors = await asyncio.wait_for(child.communicate(json.dumps(owner_options).encode()), 10)
        assert child.returncode == 0, errors.decode()
        assert output.decode().strip() == "False False False"
        assert await owner.healthy()
    finally:
        await owner.close()


async def test_db_lock_loss_disables_old_process(owner_options, postgres_store):
    owner = RuntimeOwnership(**owner_options)
    assert await owner.acquire()
    try:
        pid = owner.connection.get_server_pid()
        await postgres_store.pool.execute("SELECT pg_terminate_backend($1)", pid)
        assert not await owner.healthy()
        assert not await owner.acquire()
        assert not await owner.healthy()
    finally:
        await owner.close()


async def test_lock_files_shared_by_deployment(owner_options):
    owner = RuntimeOwnership(**owner_options)
    assert await owner.acquire()
    file = Path(owner_options["lock_path"])
    inode = file.stat().st_ino
    await owner.close()
    assert file.is_file()
    assert file.stat().st_ino == inode
    replacement = RuntimeOwnership(**owner_options)
    assert await replacement.acquire()
    try:
        assert file.stat().st_ino == inode
    finally:
        await replacement.close()


async def test_different_lock_path_still_cannot_bypass_database_lock(owner_options):
    first = RuntimeOwnership(**owner_options)
    assert await first.acquire()
    second = RuntimeOwnership(**{**owner_options, "lock_path": owner_options["lock_path"] + ".wrong"})
    try:
        assert not await second.acquire()
        assert not await second.healthy()
    finally:
        await second.close()
        await first.close()


async def test_unapproved_host_cannot_take_over(owner_options, postgres_store):
    await postgres_store.pool.execute("""INSERT INTO ra_runtime_owner (deployment_id,owner_host_id,owner_run_id,state)
        VALUES ('test-deployment','other-approved-host','previous-run','RUNNING')""")
    owner = RuntimeOwnership(**owner_options)
    assert not await owner.acquire()
    assert not await owner.healthy()
    await owner.close()
