"""Deployment ownership and scheduler lifecycle."""

import asyncio
import os
from pathlib import Path
import sys
import logging
from uuid import uuid4

import asyncpg
from robot_agent.domain.models import DependencyUnavailable, PreflightBlocked


class RuntimeOwnership:
    def __init__(self, *, dsn, deployment_id, host_id, lock_path, advisory_key, schema="public"):
        self.dsn = dsn
        self.deployment_id = deployment_id
        self.host_id = host_id
        self.lock_path = lock_path
        self.advisory_key = advisory_key
        self.schema = schema
        self.run_id = str(uuid4())
        self.connection = None
        self.file = None
        self.disabled = False
        self.attempted = False
        self.query_lock = asyncio.Lock()

    async def acquire(self):
        if self.attempted or self.disabled or sys.platform != "linux":
            return False
        self.attempted = True
        import fcntl
        target = Path(self.lock_path)
        if not target.is_absolute() or not target.parent.is_dir():
            self.disabled = True
            return False
        try:
            descriptor = os.open(target, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
            self.file = os.fdopen(descriptor, "r+")
            fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.connection = await asyncpg.connect(self.dsn, server_settings={"search_path": self.schema})
            if not await self.connection.fetchval("SELECT pg_try_advisory_lock($1::bigint)", self.advisory_key):
                await self.close()
                return False
            async with self.connection.transaction():
                prior_host = await self.connection.fetchval("SELECT owner_host_id FROM ra_runtime_owner WHERE deployment_id=$1 FOR UPDATE",
                                                           self.deployment_id)
                if prior_host is not None and prior_host != self.host_id:
                    raise PermissionError("deployment belongs to another approved host")
                await self.connection.execute("""INSERT INTO ra_runtime_owner
                    (deployment_id,owner_host_id,owner_run_id,state) VALUES ($1,$2,$3,'RECOVERING')
                    ON CONFLICT (deployment_id) DO UPDATE SET owner_run_id=EXCLUDED.owner_run_id,
                    state='RECOVERING',started_at=clock_timestamp()""", self.deployment_id, self.host_id, self.run_id)
            self.connection.add_termination_listener(lambda connection: self._disable())
            return True
        except (BlockingIOError, OSError, PermissionError, asyncpg.PostgresError):
            await self.close()
            return False

    def _disable(self):
        self.disabled = True

    async def healthy(self):
        if self.disabled or self.connection is None or self.connection.is_closed() or self.file is None:
            self.disabled = True
            return False
        async with self.query_lock:
            try:
                healthy = await self.connection.fetchval("""SELECT EXISTS (
                    SELECT 1 FROM ra_runtime_owner WHERE deployment_id=$1 AND owner_host_id=$2 AND owner_run_id=$3
                    AND state IN ('RECOVERING','RUNNING')) AND EXISTS (
                    SELECT 1 FROM pg_locks WHERE locktype='advisory' AND pid=pg_backend_pid() AND granted
                    AND classid=$4::bigint AND objid=$5::bigint AND objsubid=1)""", self.deployment_id, self.host_id,
                    self.run_id, (self.advisory_key >> 32) & 0xFFFFFFFF, self.advisory_key & 0xFFFFFFFF)
                if not healthy:
                    self.disabled = True
                return bool(healthy)
            except (OSError, asyncpg.PostgresError, asyncpg.InterfaceError):
                self.disabled = True
                return False

    async def close(self):
        self.disabled = True
        if self.connection is not None:
            connection, self.connection = self.connection, None
            if not connection.is_closed():
                await connection.close()
        if self.file is not None:
            # Close the descriptor, keep the lock file/inode permanently.
            self.file.close()
            self.file = None


class Runtime:
    def __init__(self, *, owner, service, deployment_id, scan_interval):
        self.owner = owner
        self.service = service
        self.deployment_id = deployment_id
        self.scan_interval = scan_interval
        self.stopping = asyncio.Event()
        self.loop_task = None
        self.error = ""

    async def start(self):
        if not await self.owner.acquire():
            raise RuntimeError("deployment execution ownership unavailable")
        await self.service.recover(self.deployment_id)
        self.loop_task = asyncio.create_task(self._run())

    async def _run(self):
        while not self.stopping.is_set() and await self.owner.healthy():
            try:
                await self.service.expire_commands(self.deployment_id)
                await self.service.dispatch_next(self.deployment_id)
                self.error = ""
            except (DependencyUnavailable, PreflightBlocked) as exc:
                self.error = type(exc).__name__
                logging.getLogger(__name__).warning("runtime dependency unavailable: %s", self.error)
            except (OSError, asyncpg.PostgresError, asyncpg.InterfaceError):
                self.error = "STORAGE_UNAVAILABLE"
                self.owner._disable()
                return
            try:
                await asyncio.wait_for(self.stopping.wait(), self.scan_interval)
            except asyncio.TimeoutError:
                pass

    async def close(self):
        self.owner._disable()
        self.stopping.set()
        if self.loop_task is not None:
            try:
                await self.loop_task
            finally:
                await self.owner.close()
        else:
            await self.owner.close()


class SubmissionOrderQueue:
    """Approved first-release policy: durable submission order, no automatic expiry."""

    def order(self, tasks):
        # The store orders by the immutable queue_seq assigned in the admission transaction.
        return list(tasks)
