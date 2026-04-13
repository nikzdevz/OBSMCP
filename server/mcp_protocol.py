from __future__ import annotations

import json
from typing import Any

from .service import ObsmcpService


def _success(result: Any, rpc_id: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": rpc_id, "result": result}


def _error(code: int, message: str, rpc_id: Any = None) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": rpc_id, "error": {"code": code, "message": message}}


def _stringify(value: Any) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, indent=2, ensure_ascii=True)


def _structured_content(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, list):
        return {"items": value}
    if value is None:
        return {}
    return {"value": value}


def handle_rpc(service: ObsmcpService, payload: dict[str, Any]) -> dict[str, Any] | None:
    method = payload.get("method")
    params = payload.get("params", {})
    rpc_id = payload.get("id")

    try:
        if method == "initialize":
            return _success(
                {
                    "protocolVersion": "2025-03-26",
                    "serverInfo": {"name": service.config.app_name, "version": "1.0.0"},
                    "capabilities": {"tools": {}, "resources": {}},
                },
                rpc_id,
            )
        if method == "notifications/initialized":
            return None
        if method == "ping":
            return _success({"ok": True}, rpc_id)
        if method == "tools/list":
            return _success({"tools": service.list_tool_definitions()}, rpc_id)
        if method == "tools/call":
            name = params["name"]
            arguments = params.get("arguments", {})
            result = service.call_tool(name, arguments)
            return _success(
                {
                    "content": [{"type": "text", "text": _stringify(result)}],
                    "structuredContent": _structured_content(result),
                },
                rpc_id,
            )
        if method == "resources/list":
            return _success({"resources": service.list_resource_definitions()}, rpc_id)
        if method == "resources/read":
            resource = service.get_resource(
                params["uri"],
                project_path=params.get("project_path"),
                project_slug=params.get("project_slug"),
            )
            if "text" in resource:
                contents = [{"uri": resource["uri"], "mimeType": resource["mimeType"], "text": resource["text"]}]
            else:
                contents = [{"uri": resource["uri"], "mimeType": resource["mimeType"], "text": _stringify(resource["json"])}]
            return _success({"contents": contents}, rpc_id)
        return _error(-32601, f"Method not found: {method}", rpc_id)
    except KeyError as exc:
        return _error(-32602, f"Invalid parameters: {exc}", rpc_id)
    except FileNotFoundError as exc:
        return _error(-32004, f"Not found: {exc}", rpc_id)
    except ValueError as exc:
        return _error(-32602, str(exc), rpc_id)
    except Exception as exc:
        return _error(-32000, f"Server error: {exc}", rpc_id)
