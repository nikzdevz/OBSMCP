"""Starts the bundled FastAPI server locally in standalone mode.

Reuses the same app module as the remote backend; only the DB path is
different (points to the local user's SQLite file).
"""

from __future__ import annotations

import asyncio
import os

import uvicorn

from .config import Config
from .utils.logger import get_logger

logger = get_logger("obsmcp.ui")


async def serve_local_ui(config: Config) -> None:
    os.environ.setdefault("OBSMCP_DB_PATH", config.local_db_path)
    os.environ.setdefault("OBSMCP_PORT", str(config.local_ui_port))
    os.environ.setdefault("OBSMCP_HOST", "127.0.0.1")
    # In standalone mode auth is optional; leave token blank by default.

    from obsmcp_server.main import app  # noqa: WPS433 (local import by design)

    cfg = uvicorn.Config(
        app=app,
        host=os.environ["OBSMCP_HOST"],
        port=int(os.environ["OBSMCP_PORT"]),
        log_level="info",
    )
    server = uvicorn.Server(cfg)
    logger.info("Local UI listening on http://%s:%s", cfg.host, cfg.port)
    await server.serve()


def run_local_ui_blocking(config: Config) -> None:
    asyncio.run(serve_local_ui(config))
