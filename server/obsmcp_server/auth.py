"""Bearer-token auth middleware.

If ``OBSMCP_API_TOKEN`` is unset, auth is skipped (local mode).
"""

from __future__ import annotations

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import ASGIApp

from .config import get_config

_PUBLIC_PATHS: set[str] = {
    "/healthz",
    "/readyz",
    "/runtime-discovery",
    "/mode",
    "/",
}


class BearerAuthMiddleware(BaseHTTPMiddleware):
    def __init__(self, app: ASGIApp) -> None:
        super().__init__(app)

    async def dispatch(self, request: Request, call_next):  # type: ignore[override]
        cfg = get_config()
        path = request.url.path

        # Static asset paths under the frontend mount are always public
        if path in _PUBLIC_PATHS or path.startswith("/assets") or not path.startswith("/api") and not path.startswith("/ws"):
            return await call_next(request)

        if not cfg.api_token:
            # Local / no-auth mode.
            return await call_next(request)

        # SSE & WebSocket may send the token via query param since EventSource
        # does not support custom headers.
        header = request.headers.get("authorization", "")
        expected = f"Bearer {cfg.api_token}"
        if header == expected:
            return await call_next(request)
        query_token = request.query_params.get("token")
        if query_token == cfg.api_token:
            return await call_next(request)
        return JSONResponse(status_code=401, content={"detail": "Invalid or missing token"})
