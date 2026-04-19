"""OBSMCP FastAPI application."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from .auth import BearerAuthMiddleware
from .config import get_config
from .db import get_db, init_db
from .routers import (
    agents,
    blockers,
    code_atlas,
    decisions,
    events,
    knowledge_graph,
    performance_logs,
    projects,
    sessions,
    tasks,
    work_logs,
)
from .ws import router as ws_router

logger = logging.getLogger("obsmcp.server")


@asynccontextmanager
async def lifespan(app: FastAPI):  # noqa: ARG001
    cfg = get_config()
    init_db(cfg.db_path)
    logger.info("OBSMCP backend ready at db=%s mode=%s", cfg.db_path, cfg.mode)
    yield


def create_app() -> FastAPI:
    app = FastAPI(title="OBSMCP", version="0.1.0", lifespan=lifespan)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.add_middleware(BearerAuthMiddleware)

    @app.middleware("http")
    async def error_handler(request: Request, call_next):  # noqa: ANN001
        try:
            return await call_next(request)
        except Exception:  # noqa: BLE001
            logger.exception("Unhandled error on %s %s", request.method, request.url.path)
            return JSONResponse(
                status_code=500, content={"error": "internal server error"}
            )

    app.include_router(tasks.router, prefix="/api/tasks", tags=["tasks"])
    app.include_router(sessions.router, prefix="/api/sessions", tags=["sessions"])
    app.include_router(blockers.router, prefix="/api/blockers", tags=["blockers"])
    app.include_router(decisions.router, prefix="/api/decisions", tags=["decisions"])
    app.include_router(work_logs.router, prefix="/api/work-logs", tags=["work-logs"])
    app.include_router(code_atlas.router, prefix="/api/code-atlas", tags=["code-atlas"])
    app.include_router(
        knowledge_graph.router, prefix="/api/knowledge-graph", tags=["knowledge-graph"]
    )
    app.include_router(
        performance_logs.router, prefix="/api/performance-logs", tags=["performance-logs"]
    )
    app.include_router(projects.router, prefix="/api/projects", tags=["projects"])
    app.include_router(agents.router, prefix="/api/agents", tags=["agents"])
    app.include_router(events.router, prefix="/api", tags=["events"])
    app.include_router(ws_router)

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz")
    def readyz() -> dict[str, str]:
        get_db().execute("SELECT 1")
        return {"status": "ready"}

    @app.get("/runtime-discovery")
    def runtime_discovery() -> dict[str, object]:
        cfg = get_config()
        return {
            "version": "0.1.0",
            "mode": cfg.mode,
            "features": [
                "tasks",
                "sessions",
                "blockers",
                "decisions",
                "work_logs",
                "code_atlas",
                "knowledge_graph",
                "performance_logs",
            ],
            "db_schema_version": 1,
        }

    @app.get("/mode")
    def get_mode() -> dict[str, str]:
        cfg = get_config()
        return {"mode": cfg.mode}

    dist = Path(__file__).parent / "frontend_dist"
    if dist.exists():
        app.mount("/", StaticFiles(directory=str(dist), html=True), name="frontend")
    else:

        @app.get("/")
        def root() -> dict[str, str]:
            return {
                "service": "obsmcp",
                "version": "0.1.0",
                "note": "Frontend not built. Run `npm run build` in frontend/.",
            }

    return app


app = create_app()


def run() -> None:
    import uvicorn

    cfg = get_config()
    uvicorn.run(
        "obsmcp_server.main:app",
        host=cfg.host,
        port=cfg.port,
        reload=False,
    )


if __name__ == "__main__":  # pragma: no cover
    run()
