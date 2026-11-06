import json
import base64
import asyncio
import logging

import anyio
from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from robot_agent.application.pcm_segment_service import SpeechActivity
from robot_agent.domain.models import ConflictError, DependencyUnavailable, PlanError
from robot_agent.protocol.dlv0_schema import ready_frame


router = APIRouter()
log = logging.getLogger(__name__)


async def _pcm_worker(call, *args, cancel_cleanup=None):
    """Finish in-flight inference before disconnect cleanup touches its state."""
    work = asyncio.create_task(asyncio.to_thread(call, *args))
    cancelled = False
    while True:
        try:
            result = await asyncio.shield(work)
            break
        except asyncio.CancelledError:
            cancelled = True
            if work.cancelled():
                raise
        except Exception:
            if cancelled:
                raise asyncio.CancelledError from None
            raise
    if cancelled:
        if cancel_cleanup is not None:
            await _pcm_worker(cancel_cleanup, result)
        raise asyncio.CancelledError
    return result


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
    except DependencyUnavailable:
        await ws.close(code=1013)
        return
    except PermissionError:
        await ws.close(code=4401)
        return
    await ws.send_json(ready_frame(robot_id))
    session = None
    voice_session = None
    voice = getattr(ws.app.state, "voice", None)
    pcm_service = getattr(ws.app.state, "pcm_segments", None)
    pcm_session = None
    received_chunks = received_bytes = 0
    is_voice = binding.get("channel") == "voice"
    if is_voice:
        if voice is None and pcm_service is None:
            await ws.close(code=1013)
            return
        if voice is not None:
            voice_session = voice.register(binding, ws.send_json)
    try:
        while True:
            # Exactly one receive call path; command feedback never starts a second reader.
            raw = await ws.receive_text()
            if len(raw.encode()) > robot.max_frame_bytes:
                await ws.close(code=1009)
                return
            frame = json.loads(raw)
            if not isinstance(frame, dict):
                raise PlanError("expected an object frame")
            if is_voice:
                if frame.get("type") == "stats":
                    # Legacy microphone health reports do not start a voice turn.
                    continue
                if frame.get("type") == "audio":
                    if pcm_service is None:
                        await ws.close(code=1013)
                        return
                    encoded = frame.get("pcm_b64")
                    if not isinstance(encoded, str) or not encoded:
                        raise PlanError("invalid PCM frame")
                    try:
                        pcm = base64.b64decode(encoded, validate=True)
                    except (ValueError, TypeError):
                        raise PlanError("invalid PCM frame") from None
                    if not pcm:
                        raise PlanError("empty PCM frame")
                    if len(pcm) > pcm_service.config.max_chunk_bytes:
                        await ws.close(code=1009)
                        return
                    try:
                        if pcm_session is None:
                            pcm_session = await _pcm_worker(
                                pcm_service.new_session, cancel_cleanup=pcm_service.close_session)
                        events = await _pcm_worker(pcm_service.feed_pcm_events, pcm_session, pcm)
                        segments = []
                        # Freeze every utterance's eligibility before any ASR
                        # await, including multiple utterances in one packet.
                        for event in events:
                            if isinstance(event, SpeechActivity):
                                if voice_session is not None:
                                    voice.observe_pcm_activity(voice_session, event)
                            else:
                                segments.append(event)
                                if (event.end_reason == "max_duration" and voice_session is not None
                                        and (getattr(voice, "allow_unwoken_pcm", False)
                                             or getattr(voice, "wake_evaluation", None) is not None)):
                                    break
                    except (ValueError, RuntimeError, OSError):
                        log.exception("PCM segmentation failed for robot %s", robot_id)
                        await ws.send_json({"type": "state", "state": "error", "message": "PCM_PROCESSING_FAILED"})
                        await ws.close(code=1011)
                        return
                    received_chunks += 1
                    received_bytes += len(pcm)
                    if segments:
                        await ws.send_json({"type": "state", "state": "thinking"})
                        for segment in segments:
                            # Always return the actual cut WAV, including in the opt-in task mode.
                            await ws.send_json({
                                "type": "pcm_segment", "segment_id": segment.segment_id,
                                "duration_ms": segment.duration_ms, "end_reason": segment.end_reason,
                                "received_chunks": received_chunks, "received_pcm_bytes": received_bytes,
                                "wav_b64": base64.b64encode(segment.wav_bytes).decode("ascii"),
                            })
                            log.info("PCM segment robot=%s id=%s duration_ms=%s end=%s chunks=%s bytes=%s",
                                     robot_id, segment.segment_id, segment.duration_ms, segment.end_reason,
                                     received_chunks, received_bytes)
                            if voice_session is not None and (
                                getattr(voice, "allow_unwoken_pcm", False)
                                or getattr(voice, "wake_evaluation", None) is not None
                            ):
                                if segment.end_reason != "silence":
                                    # Closing also discards every trailing fragment, including those
                                    # already cut from this network frame; reconnect starts cleanly.
                                    await ws.send_json({"type": "state", "state": "error",
                                                        "message": "PCM_TOO_LONG"})
                                    await ws.close(code=1009)
                                    return
                                try:
                                    result = await voice.deliver_pcm(voice_session, segment)
                                except (DependencyUnavailable, PlanError, ConflictError) as exc:
                                    await ws.send_json({"type": "state", "state": "error", "message": str(exc)})
                                except Exception:
                                    log.exception("PCM task submission failed for robot %s segment %s",
                                                  robot_id, segment.segment_id)
                                    await ws.send_json({"type": "state", "state": "error",
                                                        "message": "PCM_TASK_SUBMISSION_FAILED"})
                                else:
                                    await ws.send_json(result)
                        await ws.send_json({"type": "state", "state": "idle"})
                elif frame.get("type") == "segment":
                    if voice_session is None:
                        await ws.close(code=1013)
                        return
                    try:
                        audio = base64.b64decode(frame.get("wav_b64", ""), validate=True)
                    except ValueError:
                        raise PlanError("invalid voice segment") from None
                    await voice.deliver(voice_session, audio, woke=frame.get("woke") is True)
                elif frame.get("type") == "interrupt":
                    continue
                else:
                    raise PlanError("expected voice segment or PCM audio")
            elif session is None:
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
        # ASGI cancellation scopes must not interrupt connection cleanup.
        with anyio.CancelScope(shield=True):
            try:
                if pcm_session is not None:
                    try:
                        await _pcm_worker(pcm_service.close_session, pcm_session)
                    except Exception:
                        log.exception("PCM cleanup failed for robot %s", robot_id)
            finally:
                try:
                    if session is not None:
                        await robot.unregister(session)
                finally:
                    if voice_session is not None:
                        voice.unregister(voice_session)
