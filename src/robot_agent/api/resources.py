"""Resource presentation only; version writes belong to Resources."""

from dataclasses import asdict

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict

from robot_agent.domain.models import ConflictError, DependencyUnavailable, PlanError


router = APIRouter()


class ResourceInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    entries: dict[str, str]
    source_refs: list[str]
    base_version: int


async def web_context(request):
    authorization = request.headers.get("authorization", "")
    if not authorization.startswith("Bearer "):
        raise HTTPException(401, "UNAUTHENTICATED")
    try:
        return await request.app.state.identity.resolve_web(authorization[7:])
    except PermissionError:
        raise HTTPException(401, "UNAUTHENTICATED") from None
    except DependencyUnavailable as exc:
        raise HTTPException(503, str(exc)) from None


def resource_kind(kind):
    if kind not in {"capabilities", "environment"}:
        raise HTTPException(404, "RESOURCE_KIND_NOT_FOUND")
    return "capability" if kind == "capabilities" else kind


@router.post("/api/robot-agent/resources/{kind}/versions", status_code=201)
async def put_resource(kind: str, body: ResourceInput, request: Request):
    context = await web_context(request)
    try:
        resources = request.app.state.resources
        binding_id = await resources.binding_for(context)
        return await resources.put(context, binding_id, resource_kind(kind), body.entries, tuple(body.source_refs),
                                   base_version=body.base_version)
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from None
    except PlanError as exc:
        raise HTTPException(422, str(exc)) from None
    except ConflictError as exc:
        raise HTTPException(409, str(exc)) from None


@router.get("/api/robot-agent/resources/snapshot")
async def get_snapshot(request: Request):
    context = await web_context(request)
    try:
        return asdict(await request.app.state.resources.snapshot(context))
    except DependencyUnavailable as exc:
        raise HTTPException(503, str(exc)) from None


@router.get("/api/robot-agent/resources/{kind}")
async def get_resource(kind: str, request: Request):
    context = await web_context(request)
    return await request.app.state.resources.read(context, resource_kind(kind))
