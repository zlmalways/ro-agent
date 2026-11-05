"""Step 27: two real WS connections, local HTTP model, production allocation/dispatch/storage."""
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
import json

import pytest
from websockets.asyncio.client import connect

from tests.acceptance.test_scheduling import add_b,platform
from tests.acceptance.test_restart_abandonment import task_until

pytestmark=pytest.mark.integration


@asynccontextmanager
async def robot_with_report(app,base,*,name,token,binding,place,busy):
    async with connect(base+'/api/voice/robot/ws?robot_id='+name,additional_headers={'x-robot-token':token},proxy=None) as ws:
        await ws.recv()
        await ws.send(json.dumps(dict(type='hello',robot_id=name,skills=['move_to'])))
        await ws.recv()
        frame=dict(type='readiness',map='模拟地图-'+name,waypoint_names=[place],nav_server_ready=True)
        if busy is not None:
            frame['busy']=busy
        await ws.send(json.dumps(frame))
        user=await app.state.identity.resolve_web('web')
        async with asyncio.timeout(4):
            while not (await app.state.resources.snapshot(user,binding)).waypoints:
                await asyncio.sleep(0.01)
        yield ws


@pytest.mark.parametrize('kind',['missing','unknown','expired'])
async def test_only_free_b_supplies_model_context_receives_command_and_completes(configuration,postgres_store,tmp_path,kind):
    add_b(configuration)
    async with platform(configuration,postgres_store,tmp_path,execute=True) as (app,base,web,prompts):
        async with robot_with_report(app,base,name='test-robot',token='device',binding='test-binding',place='A专属地点',
                                     busy=None if kind=='missing' else False) as a:
            async with robot_with_report(app,base,name='robot-b',token='device-b',binding='binding-b',place='B专属地点',busy=False) as b:
                if kind=='unknown':
                    # Current connection has no trustworthy busy evidence: the
                    # stored value belongs to a previous connection. Do not send
                    # busy:null; the current protocol correctly rejects it.
                    await postgres_store.pool.execute("""UPDATE ra_robot_bindings SET observations=jsonb_set(observations,
                        '{busy,connection_generation}',to_jsonb(connection_generation-1)) WHERE binding_id='test-binding'""")
                elif kind=='expired':
                    old=(datetime.now(timezone.utc)-timedelta(minutes=5)).isoformat()
                    await postgres_store.pool.execute("""UPDATE ra_robot_bindings SET observations=jsonb_set(observations,
                        '{busy,received_at}',to_jsonb($1::text)) WHERE binding_id='test-binding'""",old)
                user=await app.state.identity.resolve_web('web')
                candidates=await app.state.resources.online_robot_details(user)
                assert len(candidates)==2
                by_id={item.robot_binding_id:item for item in candidates}
                assert by_id['test-binding'].readiness.get('busy') is None
                assert by_id['binding-b'].readiness['busy'] is False
                assert await postgres_store.pool.fetchval('SELECT count(*) FROM ra_execution_guard')==0
                response=await web.post('/api/robot-agent/tasks',json={'text':'移动到B专属地点'},headers={'Idempotency-Key':kind})
                identifier=response.json()['task_id']
                command=json.loads(await asyncio.wait_for(b.recv(),5))
                assert command['type']=='command' and command['args']=={'location':'B专属地点'}
                assert len(prompts)==1 and prompts[0]['waypoints']==['B专属地点']
                assert 'A专属地点' not in json.dumps(prompts,ensure_ascii=False)
                with pytest.raises(TimeoutError):
                    await asyncio.wait_for(a.recv(),0.2)
                before=await task_until(web,identifier,lambda task:bool(task['result_summary']))
                assert before['state']=='RUNNING' and before['plan_robot_binding_id']=='binding-b'
                receipt=dict(type='cmd_status',cmd_id=command['cmd_id'],state='succeeded',progress=1.0)
                await b.send(json.dumps(receipt))
                final=await task_until(web,identifier,lambda task:task['state']=='SUCCEEDED')
                assert final['result_summary'][0]['robot_binding_id']=='binding-b'
                assert await postgres_store.pool.fetchval('SELECT count(*) FROM ra_commands')==1
                assert await postgres_store.pool.fetchval('SELECT count(*) FROM ra_execution_guard')==0
                (tmp_path/'only-b-flow.json').write_text(json.dumps(dict(a_state=kind,model_inputs=prompts,command=command,
                    receipt=receipt,before_receipt=before,final=final),ensure_ascii=False,indent=2),encoding='utf-8')
