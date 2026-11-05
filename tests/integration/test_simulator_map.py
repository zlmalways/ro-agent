"""Use the actual simulator's map message with production storage and preflight."""
# ruff: noqa: F811
from datetime import datetime, timedelta, timezone

import pytest

from tests.fixtures.submission import submit_and_plan
from tests.integration.test_scheduling import USER, INPUT, fleet  # noqa: F401
from tests.manual.mock_robot_ws import Simulator

pytestmark=pytest.mark.integration


async def ready(fleet):
    service, robot, resources, _, sessions, frames, connect=fleet
    resources.use_reported_waypoints=True
    resources.freshness_seconds=10
    await connect('A')
    simulated=Simulator('test-robot','map-A（模拟）',['test-table'],[],1)
    await robot.handle(sessions['A'],simulated.readiness())
    service.planner.value={'outcome':'plan','steps':[{'step_id':'s1','action':'move_to',
        'args':{'location':'test-table'},'depends_on':[]}]}
    return service,robot,resources,sessions,frames,simulated


async def test_map_load_result_is_saved_and_changed_map_cannot_run_old_plan(fleet,postgres_store):
    service,robot,resources,sessions,frames,simulated=await ready(fleet)
    snapshot=await resources.snapshot(USER,'test-binding')
    assert snapshot.readiness_evidence['preload']['value']=='loaded'
    assert snapshot.readiness_evidence['waypoints']['value']==1
    planned=await submit_and_plan(service,USER,INPUT,'old-map')
    assert planned.state=='READY'
    simulated.map_name='map-B（模拟）'
    await robot.handle(sessions['A'],simulated.readiness())
    assert list((await resources.snapshot(USER,'test-binding')).waypoints)==['test-table']
    await service.dispatch_next('test-deployment')
    blocked=await service.get(USER,planned.task_id)
    assert blocked.state=='BLOCKED' and blocked.reason_code=='RESOURCE_VERSION_CHANGED'
    assert not frames['A'] and not blocked.result_summary


@pytest.mark.parametrize('age,allowed',[(9.999,True),(10.001,False)])
async def test_ten_second_validity_boundary_uses_actual_report_timestamp(fleet,monkeypatch,age,allowed):
    service,_,resources,_,frames,_=await ready(fleet)
    snapshot=await resources.snapshot(USER,'test-binding')
    instant=datetime.fromisoformat(snapshot.readiness_evidence['map']['received_at'])+timedelta(seconds=age)
    class AtBoundary(datetime):
        @classmethod
        def now(cls,tz=None):
            return instant.astimezone(tz or timezone.utc)
    monkeypatch.setattr('robot_agent.application.resources.datetime',AtBoundary)
    current=await resources.snapshot(USER,'test-binding')
    assert bool(current.waypoints)==allowed
    view=await submit_and_plan(service,USER,INPUT,'boundary')
    assert view.state==('READY' if allowed else 'BLOCKED')
    if not allowed:
        assert view.reason_code=='ROBOT_STATE_UNKNOWN'
    assert not frames['A']
