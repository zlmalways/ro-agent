"""Step 25: current shared timestamp behavior, real PostgreSQL and adapter registration."""
# ruff: noqa: F811
from datetime import datetime, timedelta, timezone
import json

import pytest

from tests.fixtures.submission import submit_and_plan
from tests.integration.test_scheduling import USER, INPUT, fleet  # noqa: F401

pytestmark=pytest.mark.integration


@pytest.mark.parametrize('entry',['first','reconnect','repeat_hello'])
async def test_one_message_has_one_timestamp_and_first_report_can_plan(fleet,postgres_store,monkeypatch,entry):
    service,robot,resources,bindings,sessions,frames,connect=fleet
    resources.use_reported_waypoints=True
    if entry!='first':
        await connect('A')
    old_generation=sessions['A'].generation if entry!='first' else None
    instant=datetime.now(timezone.utc)
    calls=[]
    class AdvancingClock(datetime):
        @classmethod
        def now(cls,tz=None):
            value=instant+timedelta(microseconds=len(calls))
            calls.append(value)
            return value
    monkeypatch.setattr('robot_agent.adapters.postgres.datetime',AdvancingClock)
    frame=dict(type='hello',robot_id='test-robot',skills=['move_to'],map='新地图',
               waypoint_names=['首次地点'],waypoints=1,preload='loaded',busy=False,nav_server_ready=True)
    if entry=='repeat_hello':
        await robot.handle(sessions['A'],frame)
        generation=sessions['A'].generation
    else:
        async def send(value):
            frames['A'].append(value)
        session=await robot.register(bindings['A'],frame,send)
        generation=session.generation
    observations=json.loads(await postgres_store.pool.fetchval("SELECT observations FROM ra_robot_bindings WHERE binding_id='test-binding'"))
    fields=['map','waypoint_names','waypoints','preload','busy','nav_server_ready','skills']
    assert len({observations[key]['received_at'] for key in fields})==1
    assert all(observations[key]['connection_generation']==generation for key in fields)
    assert len(calls)==1
    if entry=='reconnect':
        assert generation>old_generation
    elif entry=='repeat_hello':
        assert generation==old_generation
    snapshot=await resources.snapshot(USER,'test-binding')
    assert set(snapshot.waypoints)=={'首次地点'}
    service.planner.value={'outcome':'plan','steps':[dict(step_id='s1',action='move_to',args={'location':'首次地点'},depends_on=[])]}
    monkeypatch.undo()  # Restore datetime type before ordinary JSON snapshot serialization.
    task=await submit_and_plan(service,USER,INPUT,'first-report')
    assert task.state=='READY' and task.plan_robot_binding_id=='test-binding'
    assert not frames['A']


async def test_new_connection_does_not_borrow_old_map_and_map_switch_invalidates_old_names(fleet):
    _,robot,resources,bindings,sessions,frames,connect=fleet
    resources.use_reported_waypoints=True
    await connect('A')
    await robot.handle(sessions['A'],dict(type='readiness',map='地图A',waypoint_names=['A地点']))
    assert set((await resources.snapshot(USER,'test-binding')).waypoints)=={'A地点'}
    async def send(frame):
        frames['A'].append(frame)
    session=await robot.register(bindings['A'],dict(type='hello',robot_id='test-robot',skills=['move_to'],
                                                  waypoint_names=['A地点']),send)
    assert not (await resources.snapshot(USER,'test-binding')).waypoints
    await robot.handle(session,dict(type='readiness',map='地图A',waypoint_names=['A地点']))
    assert set((await resources.snapshot(USER,'test-binding')).waypoints)=={'A地点'}
    await robot.handle(session,dict(type='readiness',map='地图B'))
    assert not (await resources.snapshot(USER,'test-binding')).waypoints
    await robot.handle(session,dict(type='readiness',map='地图A'))
    assert not (await resources.snapshot(USER,'test-binding')).waypoints
    await robot.handle(session,dict(type='readiness',map='地图B',waypoint_names=['B地点']))
    assert set((await resources.snapshot(USER,'test-binding')).waypoints)=={'B地点'}
    assert not frames['A']
