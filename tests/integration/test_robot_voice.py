import asyncio
import base64
import io
import json
import wave

import httpx
import pytest
from websockets.asyncio.client import connect

from robot_agent.adapters.dlv0 import Dlv0Robot
from robot_agent.adapters.identity import ConfiguredIdentity
from robot_agent.adapters.voice_output import VoiceOutput
from robot_agent.app import create_app
from robot_agent.application.input_service import InputService
from robot_agent.application.voice_service import VoiceService
from tests.api.test_task_audio import ASRFixture
from tests.contract.test_voice_output import TTSFixture
from tests.integration.test_robot_ws import HELLO, TestWriteGate as WriteFixture, live_app


pytestmark = pytest.mark.integration


def audio_fixture():
    output = io.BytesIO()
    with wave.open(output, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(b"\x00\x00" * 1600)
    return output.getvalue()


def voice_app(service):
    identity = ConfiguredIdentity(users={"test-user-token": {"customer_id": "test-customer", "actor_id": "test-user", "roles": ["user", "approver"]}},
                                  robots={"test-robot-token": {"customer_id": "test-customer", "binding_id": "test-binding",
                                                                  "wire_robot_id": "test-robot", "voice_wire_robot_id": "test-voice"}})
    inputs = InputService(service, ASRFixture(), max_audio_bytes=10000)
    voice = VoiceService(inputs=inputs, output=VoiceOutput(TTSFixture()), allow_woken_audio=True, max_audio_bytes=10000, duplicate_policy="per_turn")
    app = create_app(service=service, identity=identity, inputs=inputs)
    app.state.voice = voice
    app.state.robot = Dlv0Robot(service=service, write_gate=WriteFixture(), max_frame_bytes=16384)
    service.robot = app.state.robot
    app.state.robot.on_result = voice.on_result
    return app


async def test_http_voice_fallback_preserves_device_identity_and_closes_round(persisted_service):
    service, _ = persisted_service
    app = voice_app(service)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
        result = await client.post("/api/voice/robot/chat/stream", files={"audio": ("test.wav", audio_fixture(), "audio/wav")},
                                   data={"robot_id": "test-voice", "woke": "true", "member_id": "forged-user"},
                                   headers={"x-robot-token": "test-robot-token", "Idempotency-Key": "test-audio"})
        assert result.status_code == 200
        frames = [json.loads(line) for line in result.text.splitlines()]
        assert [frame["type"] for frame in frames] == ["state", "meta", "audio", "done", "state"]
        assert frames[-1]["state"] == "idle"
        assert frames[3]["member_id"] is None
        task = await service.store.pool.fetchrow("SELECT actor_kind,actor_id FROM ra_tasks")
        assert tuple(task) == ("robot", "test-binding")


async def test_asleep_audio_is_not_sent_to_asr(persisted_service):
    service, _ = persisted_service
    app = voice_app(service)
    app.state.voice.inputs.asr.transcribe = lambda audio: (_ for _ in ()).throw(AssertionError("ambient audio cannot reach ASR"))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
        result = await client.post("/api/voice/robot/chat/stream", files={"audio": ("test.wav", audio_fixture(), "audio/wav")},
                                   data={"robot_id": "test-voice", "woke": "false"}, headers={"x-robot-token": "test-robot-token"})
        assert result.status_code == 200
        assert [json.loads(line) for line in result.text.splitlines()][-1]["state"] == "idle"
        assert await service.store.pool.fetchval("SELECT count(*) FROM ra_tasks") == 0


async def test_voice_segment_and_final_result_use_separate_command_connection(persisted_service):
    service, _ = persisted_service
    app = voice_app(service)
    async with live_app(app) as base:
        async with connect(base + "/api/voice/robot/ws?robot_id=test-robot", additional_headers={"x-robot-token": "test-robot-token"}, proxy=None) as command_ws:
            await command_ws.recv()
            await command_ws.send(json.dumps(HELLO))
            await command_ws.recv()
            async with connect(base + "/api/voice/robot/ws?robot_id=test-voice", additional_headers={"x-robot-token": "test-robot-token"}, proxy=None) as voice_ws:
                await voice_ws.recv()
                await voice_ws.send(json.dumps({"type": "segment", "wav_b64": base64.b64encode(audio_fixture()).decode(), "woke": True,
                                               "member_id": "forged-user"}))
                first = [json.loads(await asyncio.wait_for(voice_ws.recv(), 3)) for _ in range(5)]
                assert first[-1]["state"] == "idle"
                assert first[2]["type"] == "audio"
                for _ in range(2):
                    await service.dispatch_next("test-deployment")
                    command = json.loads(await asyncio.wait_for(command_ws.recv(), 3))
                    await command_ws.send(json.dumps({"type": "cmd_status", "cmd_id": command["cmd_id"], "state": "succeeded"}))
                    async with asyncio.timeout(3):
                        expected = 1 if command["skill"] == "move_to" else 2
                        while await service.store.pool.fetchval("SELECT count(*) FROM ra_commands WHERE outcome='SUCCEEDED'") != expected:
                            await asyncio.sleep(0.01)
                final = [json.loads(await asyncio.wait_for(voice_ws.recv(), 3)) for _ in range(5)]
                assert final[3]["text"].startswith("机器人报告任务已完成")
                assert final[-1]["state"] == "idle"
