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
        async def expire_commands(self, deployment_id):
            pass
        async def dispatch_next(self, deployment_id):
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
