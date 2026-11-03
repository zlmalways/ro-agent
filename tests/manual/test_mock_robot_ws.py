"""Exercise the simulator over real loopback WebSockets, without provider credentials."""
import asyncio
import base64
import json
from pathlib import Path
import wave

import pytest
from websockets.asyncio.server import serve

from robot_agent.protocol.dlv0_schema import parse_frame, ready_frame
from tests.manual.mock_robot_ws import Simulator, command_session, send_wav, wav_segment


async def test_command_session_advertises_and_executes():
    loop = asyncio.get_running_loop()
    finished = loop.create_future()

    async def peer(ws):
        try:
            assert ws.request.path.endswith("?robot_id=sim-cmd")
            assert ws.request.headers["x-robot-token"] == "test"
            await ws.send(json.dumps(ready_frame("sim-cmd")))
            hello = parse_frame(json.loads(await ws.recv()))
            assert hello["skills"] == ["move_to", "vla_skill"]
            await ws.send(json.dumps(ready_frame("sim-cmd", command_channel=True)))
            state = parse_frame(json.loads(await ws.recv()))
            assert state["map"] == "模拟地图" and state["waypoint_names"] == ["前台"]
            assert state["brain_skills"] == [{"skill_id": "pick"}]

            async def command(key, location):
                await ws.send(json.dumps({"type": "command", "cmd_id": key, "skill": "move_to",
                                          "args": {"location": location}}))

            async def status():
                while True:
                    frame = parse_frame(json.loads(await ws.recv()))
                    if frame["type"] == "cmd_status":
                        return frame

            await command("one", "前台")
            assert (await status())["state"] == "running"
            assert (await status())["state"] == "succeeded"
            await command("one", "前台")
            assert (await status())["state"] == "succeeded"
            await command("bad", "不存在")
            assert (await status())["state"] == "failed"
            await command("stop", "前台")
            assert (await status())["state"] == "running"
            await ws.send(json.dumps({"type": "cancel", "cmd_id": "stop"}))
            assert (await status())["state"] == "failed"
            finished.set_result(None)
        except Exception as exc:
            finished.set_exception(exc)

    async with serve(peer, "127.0.0.1", 0) as server:
        url = f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}/api/voice/robot/ws"
        robot = Simulator("sim-cmd", "模拟地图", ["前台"], ["pick"], 0.1)
        async with asyncio.timeout(5):
            await command_session(url, "test", robot)
            await finished


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
