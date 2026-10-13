"""Application factory; production wiring is kept separate from task rules."""

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pathlib import Path
import sys

from robot_agent.adapters.identity import ConfiguredIdentity
from robot_agent.api.resources import router as resources_router
from robot_agent.api.robot_ws import router as robot_router
from robot_agent.api.tasks import router as tasks_router
from robot_agent.domain.models import DependencyUnavailable, PreflightBlocked


def create_app(*, service=None, identity=None, resources=None, inputs=None):
    if all(value is None for value in (service, identity, resources, inputs)):
        import json
        import os
        configured_file = os.environ.get("RO_AGENT_CONFIG_FILE")
        if configured_file:
            from robot_agent.deployment import build_app
            return build_app(json.loads(Path(configured_file).read_text(encoding="utf-8")))
    app = FastAPI(title="Robot Agent", version="0.1.0")
    app.state.service = service
    app.state.identity = identity or ConfiguredIdentity(users={}, robots={})
    app.state.resources = resources
    app.state.inputs = inputs
    app.include_router(tasks_router)
    app.include_router(resources_router)
    app.include_router(robot_router)
    client_dir = Path(__file__).resolve().parents[2] / "client"
    if not client_dir.is_dir():
        client_dir = Path(sys.prefix) / "share/ro-agent/client"
    app.mount("/client", StaticFiles(directory=client_dir), name="client")

    @app.get("/", include_in_schema=False)
    async def client_index():
        return FileResponse(client_dir / "index.html")

    @app.exception_handler(DependencyUnavailable)
    @app.exception_handler(PreflightBlocked)
    async def dependency_error(request: Request, exc):
        return JSONResponse(status_code=503, content={"detail": str(exc)})

    @app.get("/health")
    async def health():
        ready = app.state.service is not None
        runtime = getattr(app.state, "runtime", None)
        if runtime is not None:
            ready = ready and not runtime.error and runtime.loop_task is not None and not runtime.loop_task.done()
        return JSONResponse(status_code=200 if ready else 503,
                            content={"configured": ready, "execution_enabled": getattr(app.state, "execution_enabled", False) and ready})

    return app
