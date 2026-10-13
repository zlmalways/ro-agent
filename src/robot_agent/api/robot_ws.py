import json

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from robot_agent.domain.models import PlanError
from robot_agent.protocol.dlv0_schema import ready_frame


router = APIRouter()


@router.websocket("/api/voice/robot/ws")
async def robot_ws(ws: WebSocket):
    await ws.accept()
    robot = getattr(ws.app.state, "robot", None)
    identity = getattr(ws.app.state, "identity", None)
    if robot is None or identity is None:
        await ws.close(code=1013)
        return
    robot_id = ws.query_params.get("robot_id", "")
    try:
        binding = await identity.resolve_robot(ws.headers.get("x-robot-token", ""), robot_id)
    except PermissionError:
        await ws.close(code=4401)
        return
    await ws.send_json(ready_frame(robot_id))
    session = None
    try:
        while True:
            # Exactly one receive call path; command feedback never starts a second reader.
            raw = await ws.receive_text()
            if len(raw.encode()) > robot.max_frame_bytes:
                await ws.close(code=1009)
                return
            frame = json.loads(raw)
            if session is None:
                session = await robot.register(binding, frame, ws.send_json)
                await ws.send_json(ready_frame(robot_id, command_channel=True))
            else:
                await robot.handle(session, frame)
    except WebSocketDisconnect:
        pass
    except (PlanError, json.JSONDecodeError):
        await ws.close(code=1003)
    except PermissionError:
        await ws.close(code=4403)
    finally:
        if session is not None:
            await robot.unregister(session)
