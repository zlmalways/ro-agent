"""Actual local WS server + PostgreSQL + clearly marked protocol simulator."""

import asyncio
from contextlib import asynccontextmanager
import json
import socket

from fastapi import FastAPI
import pytest
import pytest_asyncio
import uvicorn
from websockets.asyncio.client import connect

from robot_agent.adapters.dlv0 import Dlv0Robot
from robot_agent.api.robot_ws import router
from robot_agent.domain.models import TrustedContext, TaskInput


pytestmark = pytest.mark.integration
CONTEXT = TrustedContext("test-customer", "user", "test-user")
BINDING = {"customer_id": "test-customer", "binding_id": "test-binding", "wire_robot_id": "test-robot"}
HELLO = {"type": "hello", "robot_id": "test-robot", "skills": ["move_to", "vla_skill"], "busy_cmd_id": ""}


class TestRobotIdentity:
    async def resolve_robot(self, token, robot_id):
        if token != "test-token" or robot_id != "test-robot":
            raise PermissionError("test unauthenticated")
        return dict(BINDING)


class TestWriteGate:
    async def healthy(self):
        return True


@pytest_asyncio.fixture
async def ws_robot(persisted_service):
    service, _ = persisted_service
    robot = Dlv0Robot(service=service, write_gate=TestWriteGate(), max_frame_bytes=8192)
    service.robot = robot
    return service, robot


async def connect_session(robot, **extra):
    frames = []

    async def send(frame):
        frames.append(frame)

    session = await robot.register(BINDING, {**HELLO, **extra}, send)
    return session, frames


async def test_stale_connection_does_not_update(ws_robot, postgres_store):
    service, robot = ws_robot
    old, frames = await connect_session(robot)
    view = await service.submit(CONTEXT, TaskInput("req", "web", "test", ""), "key")
    await service.dispatch_next("test-deployment")
    command_id = frames[0]["cmd_id"]
    current, _ = await connect_session(robot, busy_cmd_id=command_id)
    await robot.handle(old, {"type": "cmd_status", "cmd_id": command_id, "state": "succeeded"})
    assert (await service.get(CONTEXT, view.task_id)).result_summary[0]["outcome"] == "NONE"
    await robot.handle(current, {"type": "cmd_status", "cmd_id": command_id, "state": "succeeded"})
    assert (await service.get(CONTEXT, view.task_id)).result_summary[0]["outcome"] == "SUCCEEDED"


async def test_cross_customer_cmd_rejected(ws_robot, postgres_store):
    service, robot = ws_robot
    owner, frames = await connect_session(robot)
    view = await service.submit(CONTEXT, TaskInput("req", "web", "test", ""), "key")
    await service.dispatch_next("test-deployment")
    await postgres_store.pool.execute("""INSERT INTO ra_robot_bindings (customer_id,binding_id,wire_robot_id,enabled)
        VALUES ('other-customer','other-binding','other-robot',true)""")
    other = await robot.register({"customer_id": "other-customer", "binding_id": "other-binding", "wire_robot_id": "other-robot"},
                                 {"type": "hello", "robot_id": "other-robot", "skills": ["move_to"]}, lambda frame: None)
    await robot.handle(other, {"type": "cmd_status", "cmd_id": frames[0]["cmd_id"], "state": "succeeded"})
    assert (await service.get(CONTEXT, view.task_id)).result_summary[0]["outcome"] == "NONE"


async def test_hello_id_mismatch_rejected(ws_robot, postgres_store):
    _, robot = ws_robot
    with pytest.raises(PermissionError):
        await connect_session(robot, robot_id="different-robot")
    assert await postgres_store.pool.fetchval("SELECT connection_generation FROM ra_robot_bindings") == 1


async def test_unknown_busy_isolates(ws_robot, postgres_store):
    service, robot = ws_robot
    session, frames = await connect_session(robot, busy_cmd_id="untracked-command")
    assert await postgres_store.pool.fetchval("SELECT isolation_reason FROM ra_robot_bindings") == "UNTRACKED_EXECUTION"
    await robot.handle(session, {"type": "readiness", "busy": False, "nav_server_ready": True})
    view = await service.submit(CONTEXT, TaskInput("req", "web", "test", ""), "key")
    await service.dispatch_next("test-deployment")
    assert frames == []
    assert (await service.get(CONTEXT, view.task_id)).result_summary == ()
    assert await postgres_store.pool.fetchval("SELECT isolation_reason FROM ra_robot_bindings") == "UNTRACKED_EXECUTION"


async def test_unrelated_telemetry_does_not_refresh_readiness(ws_robot, postgres_store):
    _, robot = ws_robot
    session, _ = await connect_session(robot)
    await robot.handle(session, {"type": "readiness", "nav_server_ready": True})
    before = json.loads(await postgres_store.pool.fetchval("SELECT observations FROM ra_robot_bindings"))
    await robot.handle(session, {"type": "telemetry", "battery": 80})
    after = json.loads(await postgres_store.pool.fetchval("SELECT observations FROM ra_robot_bindings"))
    assert after["nav_server_ready"] == before["nav_server_ready"]
    assert after["battery"]["value"] == 80


@asynccontextmanager
async def live_app(app):
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    server = uvicorn.Server(uvicorn.Config(app, log_level="error", lifespan="off", ws="websockets-sansio"))
    running = asyncio.create_task(server.serve(sockets=[sock]))
    try:
        async with asyncio.timeout(5):
            while not server.started:
                if running.done():
                    running.result()
                await asyncio.sleep(0.01)
        yield "ws://127.0.0.1:" + str(sock.getsockname()[1])
    finally:
        server.should_exit = True
        await asyncio.wait_for(running, 5)
        sock.close()


async def test_authenticated_robot_ws_uses_single_read_loop(ws_robot):
    service, robot = ws_robot
    app = FastAPI()
    app.state.robot = robot
    app.state.identity = TestRobotIdentity()
    app.include_router(router)
    async with live_app(app) as base:
        async with connect(base + "/api/voice/robot/ws?robot_id=test-robot", additional_headers={"x-robot-token": "test-token"}, proxy=None) as ws:
            assert json.loads(await ws.recv()) == {"type": "state", "state": "ready", "protocol": "dlv/0", "robot_id": "test-robot"}
            await ws.send(json.dumps(HELLO))
            assert json.loads(await ws.recv())["channel"] == "command"
            view = await service.submit(CONTEXT, TaskInput("req", "web", "test", ""), "key")
            await service.dispatch_next("test-deployment")
            command = json.loads(await ws.recv())
            assert command["type"] == "command"
            await ws.send(json.dumps({"type": "cmd_status", "cmd_id": command["cmd_id"], "state": "succeeded"}))
            async with asyncio.timeout(3):
                while (await service.get(CONTEXT, view.task_id)).result_summary[0]["outcome"] != "SUCCEEDED":
                    await asyncio.sleep(0.01)
