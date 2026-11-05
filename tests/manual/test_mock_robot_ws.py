"""Exercise the simulator over real loopback WebSockets, without provider credentials."""
import asyncio
import base64
import json
from pathlib import Path
import wave

import pytest
from websockets.asyncio.server import serve

from robot_agent.protocol.dlv0_schema import parse_frame, ready_frame
from tests.manual.mock_robot_ws import Simulator, command_session, send_pcm, send_wav, wav_segment


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
