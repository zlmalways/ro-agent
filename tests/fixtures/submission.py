"""Explicit phase drivers for tests whose precondition is a planned task.

Production submit/answer only acknowledge durable input. These tests have no
runtime loop, so they deliberately invoke the real scheduler before assertions.
HTTP admission and production-runtime acceptance tests do not use these helpers.
"""

from contextlib import asynccontextmanager


async def submit_and_plan(service, context, value, key):
    view = await service.submit(context, value, key)
    if view.state == "RECEIVED":
        await service.schedule_pending(service.store.deployment_id, task_id=view.task_id)
    return await service.get(context, view.task_id)


async def answer_and_plan(service, context, task_id, clarification_id, answer, version, key):
    view = await service.answer(context, task_id, clarification_id, answer, version, key)
    if view.state == "RECEIVED":
        await service.schedule_pending(service.store.deployment_id, task_id=view.task_id)
    return await service.get(context, view.task_id)


async def wait_for_task(web, task_id, *, states=None):
    """Observe durable state after asynchronous HTTP admission."""
    import asyncio
    async with asyncio.timeout(5):
        while True:
            response = await web.get('/api/robot-agent/tasks/' + task_id)
            response.raise_for_status()
            view = response.json()
            if (view['state'] in states if states else view['state'] not in {'RECEIVED', 'PLANNING'}):
                return view
            await asyncio.sleep(0.01)




@asynccontextmanager
async def planning_loop(service):
    """Test-only driver for old UI fixtures: real planning, manually driven robot.

    Production-factory acceptance tests instead start the actual Runtime.
    """
    import asyncio
    from robot_agent.domain.models import DependencyUnavailable, PreflightBlocked

    async def run():
        while True:
            try:
                await service.schedule_pending(service.store.deployment_id)
            except (DependencyUnavailable, PreflightBlocked):
                pass
            await asyncio.sleep(0.01)

    worker = asyncio.create_task(run())
    try:
        yield
    finally:
        worker.cancel()
        try:
            await worker
        except asyncio.CancelledError:
            pass
