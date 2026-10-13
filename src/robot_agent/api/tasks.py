from dataclasses import asdict

from fastapi import APIRouter, File, HTTPException, Request, UploadFile
from pydantic import BaseModel, ConfigDict

from robot_agent.domain.models import ConflictError, DependencyUnavailable, NotFoundError, PlanError


router = APIRouter()


@router.post("/api/robot-agent/tasks", status_code=202)
async def submit(request: Request):
    auth = request.headers.get("authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(401, "UNAUTHENTICATED")
    try:
        context = await request.app.state.identity.resolve_web(auth[7:])
    except PermissionError:
        raise HTTPException(401, "UNAUTHENTICATED") from None
    body = await request.json()
    if not isinstance(body, dict) or set(body) != {"text"} or not isinstance(body["text"], str):
        raise HTTPException(422, "INVALID_TASK_INPUT")
    key = request.headers.get("idempotency-key", "")
    try:
        view = await request.app.state.service.submit(context,
                                                       request.app.state.inputs.text(request_id=key, text=body["text"]), key)
    except ConflictError as exc:
        raise HTTPException(409, str(exc)) from None
    except PlanError as exc:
        raise HTTPException(422, str(exc)) from None
    except DependencyUnavailable as exc:
        raise HTTPException(503, str(exc)) from None
    return asdict(view)


@router.post("/api/robot-agent/tasks/audio", status_code=202)
async def submit_audio(request: Request, audio: UploadFile = File(...)):
    auth = request.headers.get("authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(401, "UNAUTHENTICATED")
    try:
        context = await request.app.state.identity.resolve_web(auth[7:])
    except PermissionError:
        raise HTTPException(401, "UNAUTHENTICATED") from None
    key = request.headers.get("idempotency-key", "")
    try:
        view = await request.app.state.inputs.audio(context, await audio.read(request.app.state.inputs.max_audio_bytes + 1),
                                                    audio.content_type or "", key)
    except DependencyUnavailable as exc:
        raise HTTPException(503, str(exc)) from None
    except PlanError as exc:
        raise HTTPException(422, str(exc)) from None
    except ConflictError as exc:
        raise HTTPException(409, str(exc)) from None
    return asdict(view)


@router.get("/api/robot-agent/tasks/{task_id}")
async def get_task(task_id: str, request: Request):
    auth = request.headers.get("authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(401, "UNAUTHENTICATED")
    try:
        context = await request.app.state.identity.resolve_web(auth[7:])
        return asdict(await request.app.state.service.get(context, task_id))
    except PermissionError:
        raise HTTPException(401, "UNAUTHENTICATED") from None
    except (NotFoundError,):
        raise HTTPException(404, "NOT_FOUND") from None


class CancelInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: int


@router.post("/api/robot-agent/tasks/{task_id}/cancel")
async def cancel_task(task_id: str, body: CancelInput, request: Request):
    auth = request.headers.get("authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(401, "UNAUTHENTICATED")
    try:
        context = await request.app.state.identity.resolve_web(auth[7:])
        return asdict(await request.app.state.service.cancel(context, task_id, body.expected_version,
                                                              request.headers.get("idempotency-key", "")))
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from None
    except (ConflictError, PlanError) as exc:
        raise HTTPException(409, str(exc)) from None
    except NotFoundError:
        raise HTTPException(404, "NOT_FOUND") from None


class AnswerInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    answer: str
    expected_version: int


class DecisionInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    plan_id: str
    decision: str
    expected_version: int


@router.post("/api/robot-agent/tasks/{task_id}/clarifications/{clarification_id}/answer", status_code=202)
async def answer_task(task_id: str, clarification_id: str, body: AnswerInput, request: Request):
    from .resources import web_context
    context = await web_context(request)
    try:
        return asdict(await request.app.state.service.answer(context, task_id, clarification_id, body.answer,
                                                             body.expected_version, request.headers.get("idempotency-key", "")))
    except ConflictError as exc:
        raise HTTPException(409, str(exc)) from None
    except NotFoundError:
        raise HTTPException(404, "NOT_FOUND") from None
    except PlanError as exc:
        raise HTTPException(422, str(exc)) from None


@router.post("/api/robot-agent/tasks/{task_id}/decisions", status_code=202)
async def decide_task(task_id: str, body: DecisionInput, request: Request):
    from .resources import web_context
    context = await web_context(request)
    service = request.app.state.service
    try:
        return asdict(await service.decide(context, task_id, body.expected_version, body.decision,
                                            request.headers.get("idempotency-key", ""), plan_id=body.plan_id))
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from None
    except ConflictError as exc:
        raise HTTPException(409, str(exc)) from None
    except NotFoundError:
        raise HTTPException(404, "NOT_FOUND") from None


@router.get("/api/robot-agent/tasks/{task_id}/events")
async def task_events(task_id: str, request: Request, after: int = 0):
    from .resources import web_context
    context = await web_context(request)
    try:
        return await request.app.state.service.events(context, task_id, after)
    except NotFoundError:
        raise HTTPException(404, "NOT_FOUND") from None
    except PlanError as exc:
        raise HTTPException(422, str(exc)) from None
