"""Actual loopback timing and movement contract; no model/physical robot."""
import asyncio
from contextlib import suppress
import json

import pytest
from websockets.asyncio.server import serve

from robot_agent.protocol.dlv0_schema import parse_frame, ready_frame
from tests.manual.mock_robot_ws import Simulator, command_session, status


def test_map_fields_share_one_report_and_hello_is_identity_only():
    robot = Simulator('sim', 'mock-fridge-map（模拟）', ['冰箱旁（模拟）'], [], 0.1)
    hello, report = parse_frame(robot.hello()), parse_frame(robot.readiness())
    assert hello['executor'] == 'simulator' and hello['skills'] == ['move_to']
    assert not {'map', 'waypoint_names', 'nav_server_ready', 'busy', 'preload'} & hello.keys()
    assert report['map'] == 'mock-fridge-map（模拟）'
    assert report['waypoint_names'] == ['冰箱旁（模拟）'] and report['waypoints'] == 1
    assert report['preload'] == 'loaded' and report['nav_server_ready'] is True


@pytest.mark.parametrize('state,progress', [('running', 0.0), ('succeeded', 1.0), ('failed', 1.0)])
def test_progress_is_completion_not_success(state, progress):
    reply = status('move-1', state, 'test')
    assert reply['progress'] == progress
    assert parse_frame(reply)['state'] == state


def test_movement_cancel_unknown_and_duplicates_keep_one_execution():
    robot = Simulator('sim', '模拟地图', ['前台'], [], 30)
    def move(key, place='前台'):
        return dict(type='command', cmd_id=key, skill='move_to', args={'location':place})
    bad = robot.handle(move('bad', '不存在'), 0)
    assert bad['state'] == 'failed' and bad['progress'] == 1
    first = robot.handle(move('one'), 1)
    assert first['state'] == 'running' and first['progress'] == 0
    deadline = robot.finish_at
    assert robot.handle(move('one'), 2) == first and robot.finish_at == deadline
    assert robot.handle(dict(type='cancel', cmd_id='wrong'), 3) is None
    assert robot.active == 'one'
    cancelled = robot.handle(dict(type='cancel', cmd_id='one'), 4)
    assert cancelled['state'] == 'failed' and cancelled['progress'] == 1
    assert robot.handle(move('one'), 5) == cancelled and robot.active is None


async def test_default_first_and_second_reports_are_ten_seconds_and_receipts_immediate(tmp_path):
    accepted = asyncio.get_running_loop().create_future()
    clock = asyncio.get_running_loop().time
    async def peer(ws):
        accepted.set_result(ws)
        await ws.wait_closed()
    robot = Simulator('sim', '模拟地图', ['前台'], [], 0.1)
    records = []
    async with serve(peer, '127.0.0.1', 0) as server:
        url = 'ws://127.0.0.1:' + str(server.sockets[0].getsockname()[1]) + '/robot'
        worker = asyncio.create_task(command_session(url, 'test', robot))
        try:
            ws = await asyncio.wait_for(accepted, 3)
            await ws.send(json.dumps(ready_frame('sim')))
            hello = json.loads(await ws.recv())
            assert hello['type'] == 'hello'
            began = clock()
            await ws.send(json.dumps(ready_frame('sim', command_channel=True)))
            # No early readiness; first at 10 seconds, no artificial test clock.
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(ws.recv(), 0.2)
            first = json.loads(await asyncio.wait_for(ws.recv(), 12))
            t1 = clock() - began
            assert first['type'] == 'readiness' and 9.9 <= t1 < 13
            records.append({'elapsed':t1, 'frame':first})
            await ws.send(json.dumps(dict(type='command', cmd_id='one', skill='move_to', args={'location':'前台'})))
            for expected in ['running', 'succeeded']:
                frame = json.loads(await asyncio.wait_for(ws.recv(), 2))
                records.append({'elapsed':clock()-began, 'frame':frame})
                assert frame['type'] == 'cmd_status' and frame['state'] == expected
            second = json.loads(await asyncio.wait_for(ws.recv(), 12))
            t2 = clock() - began
            records.append({'elapsed':t2, 'frame':second})
            assert second['type'] == 'readiness' and 9.9 <= t2 - t1 < 13
            assert 19.8 <= t2 < 25
            (tmp_path/'simulator-timing.json').write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding='utf-8')
        finally:
            worker.cancel()
            with suppress(asyncio.CancelledError):
                await worker
