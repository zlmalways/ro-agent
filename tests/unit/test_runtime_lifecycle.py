import asyncio

import pytest

from robot_agent.domain.models import DependencyUnavailable
from robot_agent.runtime import Runtime


class OwnerFixture:
    def __init__(self):
        self.disabled = False
        self.closed = False

    async def healthy(self):
        return not self.disabled

    def _disable(self):
        self.disabled = True

    async def close(self):
        self.closed = True


async def test_runtime_retries_scanning_after_dependency_failure():
    class ServiceFixture:
        calls = 0
        def raise_background_error(self):
            pass
        async def close_work(self):
            pass
        async def expire_commands(self, deployment_id):
            pass
        async def schedule_pending(self, deployment_id, *, wait_for_plan):
            pass
        async def dispatch_next(self, deployment_id, *, wait_for_send):
            self.calls += 1
            if self.calls == 1:
                raise DependencyUnavailable("test resources temporarily unavailable")
            runtime.stopping.set()
    service = ServiceFixture()
    runtime = Runtime(owner=OwnerFixture(), service=service, deployment_id="test", scan_interval=0.01)
    await asyncio.wait_for(runtime._run(), 1)
    assert service.calls == 2


async def test_close_releases_owner_even_if_scheduler_failed():
    owner = OwnerFixture()
    runtime = Runtime(owner=owner, service=None, deployment_id="test", scan_interval=0.01)
    async def failed():
        raise RuntimeError("test scheduler failed")
    runtime.loop_task = asyncio.create_task(failed())
    await asyncio.sleep(0)
    with pytest.raises(RuntimeError):
        await runtime.close()
    assert owner.closed


@pytest.mark.asyncio
async def test_runtime_recovers_after_database_connection_probe_succeeds():
    class StoreFixture:
        write_available = False
        probes = 0

        async def probe_connection(self):
            self.probes += 1
            self.write_available = True
            return True

    class ServiceFixture:
        def __init__(self):
            self.store = StoreFixture()
            self.calls = 0

        def raise_background_error(self):
            pass

        async def close_work(self):
            pass

        async def expire_commands(self, deployment_id):
            pass

        async def schedule_pending(self, deployment_id, *, wait_for_plan):
            pass

        async def dispatch_next(self, deployment_id, *, wait_for_send):
            self.calls += 1
            if self.calls == 1:
                raise ConnectionError("database connection lost")
            runtime.stopping.set()

    service = ServiceFixture()
    runtime = Runtime(owner=OwnerFixture(), service=service, deployment_id="test", scan_interval=0.001,
                      db_probe_interval=0.001, db_probe_attempts=5)
    await asyncio.wait_for(runtime._run(), 1)
    assert service.store.probes == 1
    assert runtime.error == ""
    assert not runtime.db_probe_exhausted
    assert service.calls == 2


@pytest.mark.asyncio
async def test_runtime_stops_after_five_database_probe_failures():
    class StoreFixture:
        write_available = False
        probes = 0

        async def probe_connection(self):
            self.probes += 1
            return False

    class ServiceFixture:
        def __init__(self):
            self.store = StoreFixture()

        def raise_background_error(self):
            pass

        async def close_work(self):
            pass

        async def expire_commands(self, deployment_id):
            pass

        async def schedule_pending(self, deployment_id, *, wait_for_plan):
            raise ConnectionError("database connection lost")

        async def dispatch_next(self, deployment_id, *, wait_for_send):
            pass

    owner = OwnerFixture()
    service = ServiceFixture()
    runtime = Runtime(owner=owner, service=service, deployment_id="test", scan_interval=0.001,
                      db_probe_interval=0.001, db_probe_attempts=5)
    await asyncio.wait_for(runtime._run(), 1)
    assert service.store.probes == 5
    assert runtime.error == "DATABASE_UNAVAILABLE"
    assert runtime.db_probe_exhausted
    assert owner.disabled
