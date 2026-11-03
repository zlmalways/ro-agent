"""Deployment ownership with native file locks, separate processes and PostgreSQL."""

import asyncio
import json
import os
from pathlib import Path
import sys

import asyncpg
import pytest

from robot_agent.adapters.dlv0 import Dlv0Robot
from robot_agent.runtime import RuntimeOwnership


pytestmark = pytest.mark.integration


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
        # A different DB key ensures this contender is stopped by the file lock alone.
        contender = {**owner_options, "advisory_key": owner_options["advisory_key"] + 1}
        output, errors = await asyncio.wait_for(child.communicate(json.dumps(contender).encode()), 10)
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
        connection = owner.connection
        assert not await owner.healthy()
        assert not await owner.acquire()
        assert owner.connection is connection
        robot = Dlv0Robot(service=None, write_gate=owner, max_frame_bytes=8192)
        assert not (await robot.send(None)).sent
        assert not (await robot.cancel(None)).sent
    finally:
        await owner.close()
    replacement = RuntimeOwnership(**owner_options)
    try:
        assert await replacement.acquire()
        assert await replacement.healthy()
    finally:
        await replacement.close()


async def test_driver_error_during_lock_check_permanently_revokes_owner(owner_options, monkeypatch):
    owner = RuntimeOwnership(**owner_options)
    assert await owner.acquire()
    original_fetchval = asyncpg.Connection.fetchval

    async def fail_lock_check(connection, query, *args, **kwargs):
        if connection is owner.connection and "pg_locks" in query:
            raise asyncpg.InternalClientError("connection terminated during lock check")
        return await original_fetchval(connection, query, *args, **kwargs)

    try:
        with monkeypatch.context() as patch:
            patch.setattr(asyncpg.Connection, "fetchval", fail_lock_check)
            assert not await owner.healthy()
        # The connection works again here, but this process must stay disabled.
        assert not await owner.healthy()
        assert not await owner.acquire()
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


async def test_refuses_lock_file_linked_to_another_file(owner_options, tmp_path):
    other = tmp_path / "another-file"
    other.write_bytes(b"existing data")
    Path(owner_options["lock_path"]).hardlink_to(other)
    owner = RuntimeOwnership(**owner_options)
    try:
        assert not await owner.acquire()
        assert not await owner.healthy()
        assert other.read_bytes() == b"existing data"
    finally:
        await owner.close()


async def test_lock_path_cannot_be_replaced_undetected(owner_options, tmp_path):
    owner = RuntimeOwnership(**owner_options)
    assert await owner.acquire()
    try:
        substitute = tmp_path / "substitute.lock"
        substitute.touch()
        try:
            substitute.replace(owner_options["lock_path"])
        except PermissionError:
            # Windows normally denies replacement while the file is open.
            assert await owner.healthy()
        else:
            # On filesystems that allow replacement, the old process must stop.
            assert not await owner.healthy()
            assert not await owner.acquire()
    finally:
        await owner.close()
