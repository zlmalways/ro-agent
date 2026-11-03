"""Exercise the simulator over real loopback WebSockets, without provider credentials."""
import asyncio
import base64
from contextlib import asynccontextmanager
import json
from pathlib import Path
import wave

import pytest
from websockets.asyncio.client import ClientConnection
from websockets.asyncio.server import serve

from robot_agent.protocol.dlv0_schema import parse_frame, ready_frame
from tests.manual.mock_robot_ws import Simulator, command_session, send_wav, wav_segment


@asynccontextmanager
async def connected_simulator(robot):
    accepted = asyncio.get_running_loop().create_future()

    async def peer(ws):
        accepted.set_result(ws)
        await ws.wait_closed()

    async with asyncio.timeout(10), serve(peer, "127.0.0.1", 0) as server:
        url = f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}/api/voice/robot/ws"
        client = asyncio.create_task(command_session(url, "test", robot))
        try:
            ws = await accepted
            assert ws.request.path.endswith("?robot_id=sim-cmd")
            assert ws.request.headers["x-robot-token"] == "test"
            await ws.send(json.dumps(ready_frame("sim-cmd")))
            hello = parse_frame(json.loads(await ws.recv()))
            assert hello["skills"] == ["move_to", "vla_skill"]
            await ws.send(json.dumps(ready_frame("sim-cmd", command_channel=True)))
            report = parse_frame(json.loads(await ws.recv()))
            assert report["map"] == "模拟地图" and report["waypoint_names"] == ["前台"]
            assert report["brain_skills"] == [{"skill_id": "pick"}]
            yield ws
        finally:
            server.close()
            await client


async def send_move(ws, command_id, location="前台"):
    await ws.send(json.dumps({"type": "command", "cmd_id": command_id, "skill": "move_to",
                              "args": {"location": location}}))


async def next_status(ws, command_id):
    async for raw in ws:
        frame = parse_frame(json.loads(raw))
        if frame["type"] == "cmd_status":
            assert frame["cmd_id"] == command_id
            return frame
    raise AssertionError("connection closed before command status")


async def test_cancel_stops_only_matching_command_and_replays_its_final_status():
    robot = Simulator("sim-cmd", "模拟地图", ["前台"], ["pick"], 1.0)
    async with connected_simulator(robot) as ws:
        await send_move(ws, "bad", "不存在")
        assert (await next_status(ws, "bad"))["state"] == "failed"
        await ws.send(json.dumps({"type": "command", "cmd_id": "bad-skill", "skill": "vla_skill",
                                  "args": {"skill_id": "missing"}}))
        assert (await next_status(ws, "bad-skill"))["state"] == "failed"

        await send_move(ws, "move")
        running = await next_status(ws, "move")
        assert running["state"] == "running"
        deadline = robot.finish_at
        await ws.send(json.dumps({"type": "cancel", "cmd_id": "unrelated"}))
        await send_move(ws, "move")
        assert await next_status(ws, "move") == running
        assert robot.finish_at == deadline

        await ws.send(json.dumps({"type": "cancel", "cmd_id": "move"}))
        stopped = await next_status(ws, "move")
        assert (stopped["state"], stopped["detail"]) == ("failed", "cancelled by platform")
        await asyncio.sleep(max(0, deadline - asyncio.get_running_loop().time()) + 0.02)
        await send_move(ws, "move")
        assert await next_status(ws, "move") == stopped
        assert robot.active is None


async def test_cancel_while_sending_success_keeps_success(monkeypatch):
    sending, release = asyncio.Event(), asyncio.Event()
    original_send = ClientConnection.send

    async def hold_first_success(connection, message, *args, **kwargs):
        if json.loads(message).get("state") == "succeeded" and not sending.is_set():
            sending.set()
            await release.wait()
        return await original_send(connection, message, *args, **kwargs)

    monkeypatch.setattr(ClientConnection, "send", hold_first_success)
    robot = Simulator("sim-cmd", "模拟地图", ["前台"], ["pick"], 0.05)
    async with connected_simulator(robot) as ws:
        try:
            await send_move(ws, "move")
            assert (await next_status(ws, "move"))["state"] == "running"
            await sending.wait()
            await ws.send(json.dumps({"type": "cancel", "cmd_id": "move"}))
        finally:
            release.set()
        success = await next_status(ws, "move")
        assert success["state"] == "succeeded"
        assert await next_status(ws, "move") == success
        await send_move(ws, "move")
        assert await next_status(ws, "move") == success
        assert robot.active is None


async def test_upload_is_one_wav_frame_on_authenticated_voice_channel(tmp_path):
    sample = tmp_path / "speech.wav"
    with wave.open(str(sample), "wb") as wav:
        wav.setparams((1, 2, 16000, 0, "NONE", ""))
        wav.writeframes(b"\x20\x00" * 1600)
    packets = []

    async def peer(ws):
        assert ws.request.path.endswith("?robot_id=sim-voice")
        assert ws.request.headers["x-robot-token"] == "test"
        await ws.send(json.dumps(ready_frame("sim-voice")))
        packets.append(json.loads(await ws.recv()))
        await ws.send(json.dumps({"type": "state", "state": "idle"}))

    async with serve(peer, "127.0.0.1", 0) as server:
        url = f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}/api/voice/robot/ws"
        await send_wav(url, "test", "sim-voice", sample, timeout=5)
    assert len(packets) == 1 and packets[0]["type"] == "segment" and packets[0]["woke"] is True
    assert base64.b64decode(packets[0]["wav_b64"], validate=True) == sample.read_bytes()


def test_wrong_rate_is_rejected_before_upload(tmp_path):
    sample = tmp_path / "wrong-rate.wav"
    with wave.open(str(sample), "wb") as wav:
        wav.setparams((1, 2, 48000, 0, "NONE", ""))
        wav.writeframes(b"\x00\x00" * 48)
    with pytest.raises(ValueError, match="16 kHz"):
        wav_segment(sample)


async def test_server_asr_error_is_reported():
    sample = Path(__file__).parent / "fixtures" / "front-desk.wav"

    async def reject(ws):
        await ws.send(json.dumps(ready_frame("sim-voice")))
        await ws.recv()
        await ws.send(json.dumps({"type": "state", "state": "error", "message": "ASR_UNAVAILABLE"}))
        await ws.send(json.dumps({"type": "state", "state": "idle"}))

    async with serve(reject, "127.0.0.1", 0) as server:
        url = f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}/api/voice/robot/ws"
        with pytest.raises(RuntimeError, match="ASR_UNAVAILABLE"):
            await send_wav(url, "test", "sim-voice", sample, timeout=5)
