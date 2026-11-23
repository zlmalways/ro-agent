import json
import base64
import asyncio
import logging
from uuid import UUID

import anyio
from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from robot_agent.application.pcm_segment_service import SpeechActivity
from robot_agent.domain.models import ConflictError, DependencyUnavailable, NotFoundError, PlanError
from robot_agent.protocol.dlv0_schema import ready_frame
from robot_agent.protocol.voice_schema import input_error_frame
from robot_agent.timing import TimePoint, record_timing


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
    robot_id = ws.query_params.get("robot_id", "")
    binding = None
    session = None
    voice_session = None
    is_voice = False
    send_lock = asyncio.Lock()

    async def send_json(frame):
        # Task notifications and input receipts share one ordered socket writer.
        async with send_lock:
            await ws.send_json(frame)

    def log_ws_event(event, *, close_code=None, close_reason=None, error_type=None,
                     level="info", **details):
        payload = {
            "event": event,
            "robot_id": robot_id,
            "customer_id": binding.get("customer_id") if binding else None,
            "binding_id": binding.get("binding_id") if binding else None,
            "generation": getattr(session, "generation", None),
            "channel": binding.get("channel") if binding else None,
            "close_code": close_code,
            "close_reason": close_reason or None,
            "error_type": error_type,
            **details,
        }
        getattr(log, level)("ROBOT_WS_EVENT %s", json.dumps(payload, ensure_ascii=False, sort_keys=True))

    robot = getattr(ws.app.state, "robot", None)
    identity = getattr(ws.app.state, "identity", None)
    if robot is None or identity is None:
        log_ws_event("server_close", close_code=1013, close_reason="service_unavailable", level="error")
        await ws.close(code=1013)
        return
    try:
        binding = await identity.resolve_robot(ws.headers.get("x-robot-token", ""), robot_id)
    except DependencyUnavailable:
        log_ws_event("server_close", close_code=1013, close_reason="robot_dependency_unavailable",
                     error_type="DependencyUnavailable", level="error")
        await ws.close(code=1013)
        return
    except PermissionError:
        log_ws_event("server_close", close_code=4401, close_reason="robot_authentication_failed",
                     error_type="PermissionError", level="error")
        await ws.close(code=4401)
        return
    await send_json(ready_frame(robot_id))
    voice = getattr(ws.app.state, "voice", None)
    pcm_service = getattr(ws.app.state, "pcm_segments", None)
    pcm_session = None
    pcm_capture_id = None
    consumed_capture_id = None
    received_chunks = received_bytes = 0
    is_voice = binding.get("channel") == "voice"
    if is_voice:
        if voice is None and pcm_service is None:
            log_ws_event("server_close", close_code=1013, close_reason="voice_service_unavailable", level="error")
            await ws.close(code=1013)
            return
        if voice is not None:
            voice_session = voice.register(binding, send_json)
    try:
        while True:
            # Exactly one receive call path; command feedback never starts a second reader.
            raw = await ws.receive_text()
            received_at = TimePoint.capture() if is_voice else None
            if len(raw.encode()) > robot.max_frame_bytes:
                log_ws_event("server_close", close_code=1009, close_reason="frame_too_large",
                             level="error", frame_bytes=len(raw.encode()), max_frame_bytes=robot.max_frame_bytes)
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
                    # A capture label groups PCM; the first packet is processed
                    # immediately. It never opens or grants a wake permission.
                    capture_id = frame.get("capture_id")
                    if capture_id is not None:
                        try:
                            if not isinstance(capture_id, str) or str(UUID(capture_id)) != capture_id:
                                raise ValueError
                        except (ValueError, AttributeError):
                            raise PlanError("invalid PCM capture_id") from None
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
                    if capture_id is not None and capture_id == consumed_capture_id:
                        # The complete-utterance receipt closes this capture.
                        # Packets already in transit must not create another task.
                        continue
                    try:
                        if capture_id != pcm_capture_id:
                            if pcm_session is not None:
                                await _pcm_worker(pcm_service.close_session, pcm_session)
                                pcm_session = None
                            if voice_session is not None:
                                voice.reset_pcm_activity(voice_session)
                            pcm_capture_id = capture_id
                        if pcm_session is None:
                            pcm_session = await _pcm_worker(
                                pcm_service.new_session, cancel_cleanup=pcm_service.close_session)
                        events = await _pcm_worker(pcm_service.feed_pcm_events, pcm_session, pcm, received_at)
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
                        await send_json(input_error_frame("PCM_PROCESSING_FAILED", capture_id=capture_id))
                        await ws.close(code=1011)
                        return
                    received_chunks += 1
                    received_bytes += len(pcm)
                    if segments:
                        if capture_id is not None:
                            consumed_capture_id = capture_id
                            segments = segments[:1]
                        for segment in segments:
                            first = segment.first_speech_received
                            last = segment.last_speech_received
                            finished = segment.completed_at
                            record_timing(
                                "pcm_segment", at=finished, customer_id=binding["customer_id"],
                                actor_id=binding["binding_id"], segment_id=segment.segment_id,
                                first_speech_received_at=first.utc if first else None,
                                last_speech_received_at=last.utc if last else None,
                                segment_completed_at=finished.utc if finished else None,
                                speech_receive_span_ms=last.elapsed_ms(first) if first and last else None,
                                endpoint_delay_ms=finished.elapsed_ms(last) if finished and last else None,
                                first_speech_to_segment_ms=finished.elapsed_ms(first) if finished and first else None,
                                audio_duration_ms=segment.duration_ms, end_reason=segment.end_reason,
                            )
                        await send_json({"type": "state", "state": "thinking",
                                         **({"capture_id": capture_id} if capture_id is not None else {})})
                        for segment in segments:
                            # Always return the actual cut WAV, including in the opt-in task mode.
                            await send_json({
                                "type": "pcm_segment", "segment_id": segment.segment_id,
                                **({"capture_id": capture_id} if capture_id is not None else {}),
                                "duration_ms": segment.duration_ms, "end_reason": segment.end_reason,
                                "received_chunks": received_chunks, "received_pcm_bytes": received_bytes,
                                "wav_b64": base64.b64encode(segment.wav_bytes).decode("ascii"),
                            })
                            log.debug("PCM segment robot=%s id=%s duration_ms=%s end=%s chunks=%s bytes=%s",
                                      robot_id, segment.segment_id, segment.duration_ms, segment.end_reason,
                                      received_chunks, received_bytes)
                            if voice_session is not None and (
                                getattr(voice, "allow_unwoken_pcm", False)
                                or getattr(voice, "wake_evaluation", None) is not None
                            ):
                                if segment.end_reason != "silence":
                                    # Closing also discards every trailing fragment, including those
                                    # already cut from this network frame; reconnect starts cleanly.
                                    await send_json(input_error_frame("PCM_TOO_LONG", segment_id=segment.segment_id,
                                                                      capture_id=capture_id))
                                    await ws.close(code=1009)
                                    return
                                try:
                                    result = await voice.deliver_pcm(voice_session, segment, capture_id=capture_id)
                                except (DependencyUnavailable, PlanError, ConflictError) as exc:
                                    await send_json(input_error_frame(str(exc), segment_id=segment.segment_id,
                                                                      capture_id=capture_id))
                                except Exception:
                                    log.exception("PCM task submission failed for robot %s segment %s",
                                                  robot_id, segment.segment_id)
                                    await send_json(input_error_frame("PCM_TASK_SUBMISSION_FAILED",
                                                                      segment_id=segment.segment_id,
                                                                      capture_id=capture_id))
                                else:
                                    await send_json(result)
                                    if result["type"] == "pcm_task":
                                        voice.watch_task(voice_session, result)
                elif frame.get("type") == "segment":
                    if voice_session is None:
                        await ws.close(code=1013)
                        return
                    try:
                        audio = base64.b64decode(frame.get("wav_b64", ""), validate=True)
                    except ValueError:
                        raise PlanError("invalid voice segment") from None
                    capture_id = frame.get("capture_id")
                    if capture_id is not None:
                        try:
                            if not isinstance(capture_id, str) or str(UUID(capture_id)) != capture_id:
                                raise ValueError
                        except (ValueError, AttributeError):
                            raise PlanError("invalid capture_id") from None
                    await voice.deliver(voice_session, audio, woke=frame.get("woke") is True,
                                        capture_id=capture_id)
                elif frame.get("type") == "text":
                    if voice_session is None:
                        await ws.close(code=1013)
                        return
                    request_key = frame.get("request_key")
                    text = frame.get("text")
                    conversation_id = frame.get("conversation_id")
                    if not isinstance(request_key, str) or not request_key:
                        raise PlanError("invalid text request_key")
                    if not isinstance(text, str) or not text.strip():
                        raise PlanError("invalid text")
                    if conversation_id is not None and not isinstance(conversation_id, str):
                        raise PlanError("invalid conversation_id")
                    try:
                        await voice.deliver_text(voice_session, text,
                                                 request_key=request_key,
                                                 conversation_id=conversation_id)
                    except (DependencyUnavailable, PlanError, ConflictError, NotFoundError) as exc:
                        await send_json({**input_error_frame(str(exc)), "request_key": request_key})
                elif frame.get("type") == "interrupt":
                    continue
                else:
                    raise PlanError("expected voice segment or PCM audio")
            elif session is None:
                session = await robot.register(binding, frame, send_json)
                await send_json(ready_frame(robot_id, command_channel=True))
            else:
                await robot.handle(session, frame)
    except WebSocketDisconnect as exc:
        close_code = getattr(exc, "code", None)
        log_ws_event("peer_disconnect", close_code=close_code,
                     close_reason=getattr(exc, "reason", None), error_type=type(exc).__name__,
                     level="info" if close_code in {1000, 1001} else "warning")
    except (PlanError, json.JSONDecodeError) as exc:
        log_ws_event("server_close", close_code=1003, close_reason="invalid_robot_frame",
                     error_type=type(exc).__name__, level="error")
        await ws.close(code=1003)
    except PermissionError as exc:
        log_ws_event("server_close", close_code=4403, close_reason="robot_permission_denied",
                     error_type=type(exc).__name__, level="error")
        await ws.close(code=4403)
    except Exception as exc:
        log_ws_event("server_error", error_type=type(exc).__name__, level="error")
        log.exception("Robot WebSocket failed for robot %s", robot_id)
        raise
    finally:
        log_ws_event("cleanup_started", level="debug")
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
        log_ws_event("cleanup_finished", level="debug")
