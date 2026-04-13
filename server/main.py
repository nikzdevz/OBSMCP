from __future__ import annotations

import argparse
import os
import time
from typing import Any

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from .config import load_config
from .mcp_protocol import handle_rpc
from .observability import get_logger, set_request_context, clear_request_context, generate_request_id
from .service import ObsmcpService
from .utils import configure_logging, write_text_atomic


def build_app(config_path: str | None = None) -> FastAPI:
    config = load_config(config_path)
    log_cfg = config.logging
    logger = configure_logging(
        config.log_dir,
        debug=log_cfg.level == "DEBUG" if log_cfg else False,
        json_output=log_cfg.json_output if log_cfg else False,
        json_output_path=log_cfg.json_output_path if log_cfg else None,
        include_traceback=log_cfg.include_traceback if log_cfg else False,
        console_output=log_cfg.console_output if log_cfg else True,
    )
    service = ObsmcpService(config)

    app = FastAPI(title=config.app_name, version="1.0.0", description=config.description)
    app.state.config = config
    app.state.service = service
    app.state.logger = logger

    @app.middleware("http")
    async def require_optional_token(request: Request, call_next):
        token = config.api_token
        if token and request.url.path not in {"/healthz"}:
            header = request.headers.get("authorization", "")
            if header != f"Bearer {token}":
                return JSONResponse(status_code=401, content={"detail": "Invalid or missing bearer token."})
        return await call_next(request)

    @app.get("/")
    async def root() -> dict[str, Any]:
        return {
            "name": config.app_name,
            "description": config.description,
            "port": config.port,
            "mcp_endpoint": "/mcp",
            "health_endpoint": "/healthz",
        }

    @app.get("/healthz")
    async def healthz() -> dict[str, Any]:
        return service.health_check()

    @app.post("/mcp")
    async def mcp(request: Request) -> JSONResponse:
        payload = await request.json()
        request_id = generate_request_id()
        request_json_id = str(payload.get("id", request_id)) if isinstance(payload, dict) else request_id

        set_request_context(request_id=request_json_id)

        def rpc_label(item: dict[str, Any]) -> str:
            method = item.get("method", "unknown")
            if method == "tools/call":
                return f"tools/call:{item.get('params', {}).get('name', 'unknown')}"
            return str(method)

        if isinstance(payload, list):
            responses = []
            for item in payload:
                started = time.perf_counter()
                req_id = generate_request_id()
                req_json_id = str(item.get("id", req_id))
                set_request_context(request_id=req_json_id)
                try:
                    response = await run_in_threadpool(handle_rpc, service, item)
                    logger.info("MCP %s completed in %.3fs", rpc_label(item), time.perf_counter() - started)
                except Exception as exc:
                    logger.exception("MCP %s failed: %s", rpc_label(item), exc)
                    response = {"jsonrpc": "2.0", "id": item.get("id"), "error": {"code": -32000, "message": str(exc)}}
                responses.append(response)
            clear_request_context()
            return JSONResponse(content=[item for item in responses if item is not None])
        started = time.perf_counter()
        try:
            response = await run_in_threadpool(handle_rpc, service, payload)
        except Exception as exc:
            logger.exception("MCP %s raised from thread: %s", rpc_label(payload), exc)
            clear_request_context()
            return JSONResponse(content={"jsonrpc": "2.0", "id": payload.get("id"), "error": {"code": -32000, "message": str(exc)}})
        logger.info("MCP %s completed in %.3fs", rpc_label(payload), time.perf_counter() - started)
        if response is None:
            clear_request_context()
            return JSONResponse(content={})
        clear_request_context()
        return JSONResponse(content=response)

    @app.post("/api/sync")
    async def api_sync() -> dict[str, Any]:
        return service.sync_context()

    return app


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the obsmcp server.")
    parser.add_argument("--config", default=None, help="Path to obsmcp config JSON.")
    parser.add_argument("--project", dest="project_path", default=None, help="Default project root path. Overridden by OBSMCP_PROJECT env var or per-request project_path.")
    args = parser.parse_args()

    config = load_config(args.config)
    if args.project_path:
        config.default_project_path = args.project_path
    log_cfg = config.logging
    logger = configure_logging(
        config.log_dir,
        debug=log_cfg.level == "DEBUG" if log_cfg else False,
        json_output=log_cfg.json_output if log_cfg else False,
        json_output_path=log_cfg.json_output_path if log_cfg else None,
        include_traceback=log_cfg.include_traceback if log_cfg else False,
        console_output=log_cfg.console_output if log_cfg else True,
    )
    try:
        write_text_atomic(config.pid_file, f"{os.getpid()}\n")
        logger.info("Starting obsmcp on %s:%s", config.host, config.port)
        logger.info("Default project: %s", config.default_project_path)
        logger.info("Bootstrap default project on startup: %s", config.bootstrap_default_project_on_startup)
        app = build_app(args.config)
        uvicorn.run(app, host=config.host, port=config.port, log_level="info")
    except Exception:
        logger.exception("obsmcp failed during startup")
        raise


if __name__ == "__main__":
    main()
