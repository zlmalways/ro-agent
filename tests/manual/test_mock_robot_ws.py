"""Exercise the simulator over real loopback WebSockets, without provider credentials."""
import asyncio
import base64
from contextlib import asynccontextmanager
import json
import os
from pathlib import Path
import subprocess
import sys
import wave

import pytest
from websockets.asyncio.client import ClientConnection
from websockets.asyncio.server import serve

from robot_agent.protocol.dlv0_schema import parse_frame, ready_frame
from tests.manual.mock_robot_ws import Simulator, command_session, send_pcm, send_wav, wav_segment


CLI = (sys.executable, "-m", "tests.manual.mock_robot_ws")


@pytest.mark.parametrize("args", [("serve",), ("send-wav", "unused.wav"),
                                  ("send-pcm", "unused.wav", "--output-dir", "unused-results")])
def test_cli_requires_url_before_using_credentials_or_connecting(args, monkeypatch):
    monkeypatch.delenv("RO_AGENT_ROBOT_TOKEN", raising=False)
    result = subprocess.run([*CLI, *args], capture_output=True, text=True, timeout=10)
    assert result.returncode == 2
    assert "--url" in result.stderr.strip().splitlines()[-1]


async def test_serve_cli_connects_to_supplied_url():
    accepted = asyncio.get_running_loop().create_future()

    async def peer(ws):
        accepted.set_result(ws)
        await ws.wait_closed()

    async with serve(peer, "127.0.0.1", 0) as server:
        url = f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}/chosen-platform"
        process = await asyncio.create_subprocess_exec(
            *CLI, "--url", url, "serve", "--robot-id", "cli-robot",
            env={**os.environ, "RO_AGENT_ROBOT_TOKEN": "test"},
        )
        try:
            async with asyncio.timeout(15):
                ws = await accepted
                assert ws.request.path == "/chosen-platform?robot_id=cli-robot"
                assert ws.request.headers["x-robot-token"] == "test"
                await ws.send(json.dumps(ready_frame("cli-robot")))
                assert parse_frame(json.loads(await ws.recv()))["type"] == "hello"
                await ws.send(json.dumps(ready_frame("cli-robot", command_channel=True)))
                assert parse_frame(json.loads(await ws.recv()))["type"] == "readiness"
        finally:
            if process.returncode is None:
                process.terminate()
            await process.wait()


@asynccontextmanager
async def connected_simulator(robot):
    accepted = asyncio.get_running_loop().create_future()

    async def peer(ws):
        accepted.set_result(ws)
        await ws.wait_closed()

    async with asyncio.timeout(10), serve(peer, "127.0.0.1", 0) as server:
        url = f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}/api/voice/robot/ws"
        client = asyncio.create_task(command_session(url, "test", robot, interval=0.05))
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
        result = await asyncio.to_thread(
            subprocess.run, [*CLI, "--url", url, "send-wav", "--voice-id", "sim-voice", str(sample)],
            env={**os.environ, "RO_AGENT_ROBOT_TOKEN": "test"}, capture_output=True, timeout=10,
        )
        assert result.returncode == 0, result.stderr
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
            await command_session(url, "test", robot, interval=0.05)
            await finished


def make_sample(path, pcm=b"\x20\x00" * 1600):
    with wave.open(str(path), "wb") as wav:
        wav.setparams((1, 2, 16000, 0, "NONE", ""))
        wav.writeframes(pcm)
    return path


async def reply_segment(ws, sample, chunks, pcm_bytes, *, idle=True):
    with wave.open(str(sample), "rb") as wav:
        duration_ms = wav.getnframes() / 16
    await ws.send(json.dumps({"type": "state", "state": "thinking"}))
    await ws.send(json.dumps({"type": "pcm_segment", "segment_id": "test-segment",
                              "duration_ms": duration_ms, "end_reason": "silence",
                              "wav_b64": base64.b64encode(sample.read_bytes()).decode(),
                              "received_chunks": chunks, "received_pcm_bytes": pcm_bytes}))
    if idle:
        await ws.send(json.dumps({"type": "state", "state": "idle"}))


async def test_pcm_uses_one_connection_raw_chunks_realtime_and_saves_result(tmp_path):
    sample = make_sample(tmp_path / "sample.wav")
    expected = b"\0" * 640 + b"\x20\x00" * 1600 + b"\0" * 640
    packets, arrivals, connections = [], [], []
    clock = asyncio.get_running_loop().time

    async def peer(ws):
        connections.append(ws)
        assert ws.request.path.endswith("?robot_id=sim-voice")
        assert ws.request.headers["x-robot-token"] == "test"
        await ws.send(json.dumps(ready_frame("sim-voice")))
        received = bytearray()
        while len(received) < len(expected):
            frame = json.loads(await ws.recv())
            assert set(frame) == {"type", "pcm_b64"} and frame["type"] == "audio"
            pcm = base64.b64decode(frame["pcm_b64"], validate=True)
            arrivals.append(clock())
            packets.append(pcm)
            received.extend(pcm)
        assert bytes(received) == expected
        await reply_segment(ws, sample, len(packets), len(received))

    out = tmp_path / "result"
    async with serve(peer, "127.0.0.1", 0) as server:
        url = f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}/api/voice/robot/ws"
        report = await send_pcm(url, "test", "sim-voice", sample, chunk_ms=20,
                                silence_ms=20, output_dir=out, timeout=5)
    assert len(connections) == 1
    assert len(packets) == 7 and all(len(p) == 640 for p in packets)
    assert arrivals[-1] - arrivals[0] >= 0.09  # 120 ms expected, scheduler tolerance.
    assert report["sent_chunks"] == 7 and report["sent_pcm_bytes"] == len(expected)
    assert report["status"] == "complete"
    assert report["segments"][0]["duration_ms"] == 100
    assert report["segments"][0]["end_reason"] == "silence"
    assert (out / "segment-001.wav").read_bytes() == sample.read_bytes()
    assert json.loads((out / "pcm-upload-report.json").read_text()) == report
    assert "wav_b64" not in (out / "pcm-upload-report.json").read_text()


async def test_thinking_stops_pcm_sender_before_the_file_is_exhausted(tmp_path):
    sample = make_sample(tmp_path / "long.wav", b"\x20\x00" * 16000)
    packets = []

    async def peer(ws):
        await ws.send(json.dumps(ready_frame("sim-voice")))
        packets.append(json.loads(await ws.recv()))
        await ws.send(json.dumps({"type": "state", "state": "thinking"}))
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(ws.recv(), 0.08)
        await reply_segment(ws, sample, 1, 640)

    async with serve(peer, "127.0.0.1", 0) as server:
        url = f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}/api/voice/robot/ws"
        result = await send_pcm(url, "test", "sim-voice", sample, silence_ms=0,
                                output_dir=tmp_path / "result", timeout=5)
    assert len(packets) == result["sent_chunks"] == 1
    assert result["stopped_by_thinking"] is True


async def test_pcm_disconnect_does_not_reconnect_or_replay(tmp_path):
    from websockets.exceptions import ConnectionClosed
    sample = make_sample(tmp_path / "sample.wav")
    connections = []

    async def peer(ws):
        connections.append(ws)
        await ws.send(json.dumps(ready_frame("sim-voice")))
        await ws.recv()
        await ws.close(code=1011, reason="test disconnect")

    async with serve(peer, "127.0.0.1", 0) as server:
        url = f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}/api/voice/robot/ws"
        with pytest.raises(ConnectionClosed):
            await send_pcm(url, "test", "sim-voice", sample, output_dir=tmp_path / "result", timeout=5)
    assert len(connections) == 1
    assert json.loads((tmp_path / "result" / "pcm-upload-report.json").read_text())["status"] == "error"


async def test_pcm_timeout_does_not_claim_silence_was_a_complete_segment(tmp_path):
    sample = make_sample(tmp_path / "silence.wav", b"\0" * 3200)

    async def peer(ws):
        await ws.send(json.dumps(ready_frame("sim-voice")))
        async for _ in ws:
            pass

    async with serve(peer, "127.0.0.1", 0) as server:
        url = f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}/api/voice/robot/ws"
        with pytest.raises(TimeoutError, match="completed PCM segment"):
            await send_pcm(url, "test", "sim-voice", sample, silence_ms=0,
                            output_dir=tmp_path / "result", timeout=0.3)
    report = json.loads((tmp_path / "result" / "pcm-upload-report.json").read_text())
    assert report["status"] == "error" and report["segments"] == []


@pytest.mark.parametrize("tts_unavailable", [False, True])
async def test_pcm_expect_task_records_task_and_keeps_tts_warning_separate(tmp_path, tts_unavailable):
    sample = make_sample(tmp_path / "sample.wav")
    expected_task = {"segment_id": "test-segment", "request_key": "pcm:test-segment",
                     "task_id": "task-one", "state": "QUEUED", "transcript": "请去前台"}

    async def peer(ws):
        await ws.send(json.dumps(ready_frame("sim-voice")))
        packet = json.loads(await ws.recv())
        assert set(packet) == {"type", "pcm_b64"}  # Expectation is local, never an authorization hint.
        await reply_segment(ws, sample, 1, 640, idle=False)
        await ws.send(json.dumps({"type": "pcm_task", **expected_task}))
        if tts_unavailable:
            await ws.send(json.dumps({"type": "state", "state": "error", "message": "TTS_UNAVAILABLE"}))
        await ws.send(json.dumps({"type": "state", "state": "idle"}))

    output = tmp_path / "result"
    async with serve(peer, "127.0.0.1", 0) as server:
        url = f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}/api/voice/robot/ws"
        result = await send_pcm(url, "test", "sim-voice", sample, silence_ms=0,
                                output_dir=output, expect_task=True, timeout=5)
    assert result["status"] == "complete" and result["expect_task"] is True
    assert result["tasks"] == [expected_task]
    assert result["warnings"] == (["TTS_UNAVAILABLE"] if tts_unavailable else [])
    assert result["sent_chunks"] == 1 and result["stopped_by_thinking"] is True
    assert json.loads((output / "pcm-upload-report.json").read_text(encoding="utf-8")) == result


async def test_pcm_expect_task_rejects_idle_when_only_segmentation_completed(tmp_path):
    sample = make_sample(tmp_path / "sample.wav")

    async def peer(ws):
        await ws.send(json.dumps(ready_frame("sim-voice")))
        await ws.recv()
        await reply_segment(ws, sample, 1, 640)

    output = tmp_path / "result"
    async with serve(peer, "127.0.0.1", 0) as server:
        url = f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}/api/voice/robot/ws"
        with pytest.raises(RuntimeError, match="without a PCM task"):
            await send_pcm(url, "test", "sim-voice", sample, silence_ms=0,
                            output_dir=output, expect_task=True, timeout=5)
    report = json.loads((output / "pcm-upload-report.json").read_text())
    assert report["status"] == "error" and len(report["segments"]) == 1 and report["tasks"] == []


@pytest.mark.parametrize("error", ["ASR_UNAVAILABLE", "PLANNER_UNAVAILABLE"])
async def test_pcm_task_real_errors_are_not_swallowed(tmp_path, error):
    sample = make_sample(tmp_path / "sample.wav")

    async def peer(ws):
        await ws.send(json.dumps(ready_frame("sim-voice")))
        await ws.recv()
        await reply_segment(ws, sample, 1, 640, idle=False)
        await ws.send(json.dumps({"type": "state", "state": "error", "message": error}))

    async with serve(peer, "127.0.0.1", 0) as server:
        url = f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}/api/voice/robot/ws"
        with pytest.raises(RuntimeError, match=error):
            await send_pcm(url, "test", "sim-voice", sample, silence_ms=0,
                            output_dir=tmp_path / "result", expect_task=True, timeout=5)


@pytest.mark.parametrize("field", ["segment_id", "request_key", "task_id", "state"])
async def test_incomplete_pcm_task_cannot_satisfy_expectation(tmp_path, field):
    sample = make_sample(tmp_path / "sample.wav")
    frame = {"type": "pcm_task", "segment_id": "test-segment", "request_key": "pcm:test-segment",
             "task_id": "task-one", "state": "QUEUED"}
    del frame[field]

    async def peer(ws):
        await ws.send(json.dumps(ready_frame("sim-voice")))
        await ws.recv()
        await reply_segment(ws, sample, 1, 640, idle=False)
        await ws.send(json.dumps(frame))

    async with serve(peer, "127.0.0.1", 0) as server:
        url = f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}/api/voice/robot/ws"
        with pytest.raises(ValueError, match="PCM task"):
            await send_pcm(url, "test", "sim-voice", sample, silence_ms=0,
                            output_dir=tmp_path / "result", expect_task=True, timeout=5)


async def test_pcm_task_from_another_segment_is_not_accepted(tmp_path):
    sample = make_sample(tmp_path / "sample.wav")

    async def peer(ws):
        await ws.send(json.dumps(ready_frame("sim-voice")))
        await ws.recv()
        await reply_segment(ws, sample, 1, 640, idle=False)
        await ws.send(json.dumps({"type": "pcm_task", "segment_id": "unrelated-segment",
                                  "request_key": "another-request", "task_id": "another-task", "state": "QUEUED"}))

    output = tmp_path / "result"
    async with serve(peer, "127.0.0.1", 0) as server:
        url = f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}/api/voice/robot/ws"
        with pytest.raises(ValueError, match="unknown segment"):
            await send_pcm(url, "test", "sim-voice", sample, silence_ms=0,
                            output_dir=output, expect_task=True, timeout=5)
    report = json.loads((output / "pcm-upload-report.json").read_text(encoding="utf-8"))
    assert report["status"] == "error" and report["tasks"] == []


@pytest.mark.parametrize("expect_task", [False, True])
async def test_send_pcm_cli_uses_explicit_url_and_task_expectation(tmp_path, expect_task):
    sample = make_sample(tmp_path / "sample.wav")
    packets = []

    async def peer(ws):
        assert ws.request.path == "/chosen-pcm-platform?robot_id=cli-voice"
        assert ws.request.headers["x-robot-token"] == "test"
        await ws.send(json.dumps(ready_frame("cli-voice")))
        packet = json.loads(await ws.recv())
        assert packet["type"] == "audio" and set(packet) == {"type", "pcm_b64"}
        packets.append(packet)
        await reply_segment(ws, sample, 1, 640, idle=False)
        if expect_task:
            await ws.send(json.dumps({"type": "pcm_task", "segment_id": "test-segment",
                                      "request_key": "test-segment", "task_id": "cli-task", "state": "RECEIVED"}))
        await ws.send(json.dumps({"type": "state", "state": "idle"}))

    output = tmp_path / "result"
    async with serve(peer, "127.0.0.1", 0) as server:
        url = f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}/chosen-pcm-platform"
        args = [*CLI, "--url", url, "send-pcm", "--voice-id", "cli-voice", str(sample),
                "--silence-ms", "0", "--output-dir", str(output), "--timeout", "5"]
        if expect_task:
            args.append("--expect-task")
        result = await asyncio.to_thread(subprocess.run, args,
                                        env={**os.environ, "RO_AGENT_ROBOT_TOKEN": "test"},
                                        capture_output=True, timeout=10)
        assert result.returncode == 0, result.stderr
    report = json.loads((output / "pcm-upload-report.json").read_text(encoding="utf-8"))
    assert report["status"] == "complete" and report["expect_task"] == expect_task
    assert bool(report["tasks"]) == expect_task
    assert len(packets) == 1 and (output / "segment-001.wav").read_bytes() == sample.read_bytes()
