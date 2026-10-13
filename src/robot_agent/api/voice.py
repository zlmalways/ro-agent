import asyncio
import json

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import StreamingResponse

from robot_agent.domain.models import DependencyUnavailable, TrustedContext


router = APIRouter()


@router.post("/api/voice/robot/chat/stream")
async def robot_voice_stream(request: Request, audio: UploadFile = File(...), robot_id: str = Form(...),
                             woke: bool = Form(False)):
    voice = getattr(request.app.state, "voice", None)
    if voice is None:
        raise HTTPException(503, "VOICE_UNAVAILABLE")
    try:
        binding = await request.app.state.identity.resolve_robot(request.headers.get("x-robot-token", ""), robot_id)
    except PermissionError:
        raise HTTPException(401, "UNAUTHENTICATED") from None
    except DependencyUnavailable:
        raise HTTPException(503, "IDENTITY_UNAVAILABLE") from None
    if binding.get("channel") != "voice":
        raise HTTPException(403, "VOICE_BINDING_REQUIRED")
    value = await audio.read(voice.max_audio_bytes + 1)
    context = TrustedContext(binding["customer_id"], "robot", binding["binding_id"])
    key = request.headers.get("idempotency-key", "")
    async def frames():
        yield json.dumps({"type": "state", "state": "thinking"}) + "\n"
        result = await voice.submit(context, robot_id, value, key, woke=woke)
        duration = 0
        for frame in result[1:]:
            if frame["type"] == "done":
                duration = frame["play_secs"]
            if frame.get("state") == "idle" and duration:
                await asyncio.sleep(duration)
            yield json.dumps(frame, ensure_ascii=False) + "\n"
    return StreamingResponse(frames(), media_type="application/x-ndjson", headers={"Cache-Control": "no-cache"})
