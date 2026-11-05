"""Admission must be durable and independent of model completion."""
import asyncio

import pytest

from robot_agent.domain.models import TaskInput, TrustedContext
from tests.api.test_tasks import api_app, HEADERS
import httpx


pytestmark = pytest.mark.integration
USER = TrustedContext('test-customer', 'user', 'test-user')


async def test_http_accepts_and_cancels_before_slow_model_returns(persisted_service, postgres_store):
    service, robot = persisted_service
    entered, release = asyncio.Event(), asyncio.Event()
    original = service.planner.plan

    async def slow(*args):
        entered.set()
        await release.wait()
        return await original(*args)

    service.planner.plan = slow
    async with httpx.AsyncClient(transport=httpx.ASGITransport(api_app(service)), base_url='http://test') as web:
        response = await asyncio.wait_for(web.post('/api/robot-agent/tasks', json={'text':'请去前台'}, headers=HEADERS), 1)
        assert response.status_code == 202
        accepted = response.json()
        assert accepted['state'] == 'RECEIVED' and accepted['task_id'] and not entered.is_set()
        assert await postgres_store.pool.fetchval('SELECT state FROM ra_tasks WHERE task_id=$1', accepted['task_id']) == 'RECEIVED'
        planning = asyncio.create_task(service.schedule_pending('test-deployment'))
        try:
            await asyncio.wait_for(entered.wait(), 1)
            current = (await web.get('/api/robot-agent/tasks/' + accepted['task_id'], headers=HEADERS)).json()
            assert current['state'] == 'PLANNING'
            stopped = await web.post('/api/robot-agent/tasks/' + accepted['task_id'] + '/cancel',
                                     json={'expected_version':current['version']}, headers={**HEADERS,'Idempotency-Key':'stop'})
            assert stopped.json()['state'] == 'CANCELLED'
        finally:
            release.set()
            await planning
        await service.dispatch_next('test-deployment')
        final = await service.get(USER, accepted['task_id'])
        assert final.state == 'CANCELLED' and final.plan_id is None
        assert robot.sent == []
        assert await postgres_store.pool.fetchval('SELECT count(*) FROM ra_commands') == 0


async def test_answer_is_saved_and_returns_before_replanning(persisted_service):
    service, _ = persisted_service
    service.planner.value = {'outcome':'clarify','question':'去哪里？','required_slots':['location']}
    task = await service.submit(USER, TaskInput('ask','web','请过去',''), 'ask')
    await service.schedule_pending('test-deployment')
    asked = await service.get(USER, task.task_id)
    assert asked.state == 'NEEDS_CLARIFICATION'

    async def must_not_run(*args):
        raise AssertionError('answer must not wait for model')

    service.planner.plan = must_not_run
    answered = await service.answer(USER, asked.task_id, asked.clarification_id, '前台', asked.version, 'answer')
    assert answered.state == 'RECEIVED'
    async with service.store.unit_of_work(USER) as tx:
        assert (await tx.load_task(task.task_id)).answers == ('前台',)
    stopped = await service.cancel(USER, answered.task_id, answered.version, 'stop-answer')
    assert stopped.state == 'CANCELLED'
