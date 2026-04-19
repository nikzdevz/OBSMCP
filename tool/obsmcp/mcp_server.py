"""MCP tool server — exposes OBSMCP functionality to any MCP-compatible client.

Uses stdio transport so it plugs into Claude Desktop, Cursor, and Claude Code
without additional configuration.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from typing import Any

from .client.http_client import BackendClient
from .config import load_config
from .scanners.code_atlas import CodeAtlasScanner
from .utils.logger import get_logger

logger = get_logger("obsmcp.mcp")


def _serialize(result: Any) -> str:
    return json.dumps(result, default=str)


async def _build_tool_handlers(client: BackendClient, config) -> dict[str, Any]:  # noqa: ANN001
    scanner = CodeAtlasScanner(client, config)

    async def get_tasks(status: str | None = None) -> str:
        return _serialize(client.list_tasks(status))

    async def create_task(
        title: str,
        description: str | None = None,
        status: str = "open",
        priority: str = "medium",
        tags: list[str] | None = None,
    ) -> str:
        task = {
            "title": title,
            "description": description,
            "status": status,
            "priority": priority,
            "tags": tags,
            "project_id": config.project_id or None,
        }
        return _serialize(await client.create_task(task))

    async def update_task(
        task_id: str,
        title: str | None = None,
        description: str | None = None,
        status: str | None = None,
        priority: str | None = None,
        tags: list[str] | None = None,
    ) -> str:
        updates: dict[str, Any] = {}
        for k, v in {
            "title": title,
            "description": description,
            "status": status,
            "priority": priority,
            "tags": tags,
        }.items():
            if v is not None:
                updates[k] = v
        return _serialize(await client.update_task(task_id, updates))

    async def delete_task(task_id: str) -> str:
        await client.delete_task(task_id)
        return _serialize({"ok": True, "id": task_id})

    async def log_blocker(description: str, severity: str = "medium") -> str:
        return _serialize(
            await client.log_blocker(
                {
                    "description": description,
                    "severity": severity,
                    "project_id": config.project_id or None,
                }
            )
        )

    async def resolve_blocker(blocker_id: str, resolution: str) -> str:
        return _serialize(await client.resolve_blocker(blocker_id, resolution))

    async def log_decision(
        decision: str,
        context: str | None = None,
        outcome: str | None = None,
        tags: list[str] | None = None,
    ) -> str:
        return _serialize(
            await client.log_decision(
                {
                    "decision": decision,
                    "context": context,
                    "outcome": outcome,
                    "tags": tags,
                    "project_id": config.project_id or None,
                }
            )
        )

    async def log_work(description: str, hours: float | None = None, tags: list[str] | None = None) -> str:
        return _serialize(
            await client.log_work(
                {
                    "description": description,
                    "hours": hours,
                    "tags": tags,
                    "project_id": config.project_id or None,
                }
            )
        )

    async def start_session(context: str | None = None) -> str:
        return _serialize(await client.start_session(config.project_id or "", context or ""))

    async def end_session(session_id: str) -> str:
        await client.end_session(session_id)
        return _serialize({"ok": True, "id": session_id})

    async def scan_codebase() -> str:
        scan_id = await scanner.perform_scan()
        return _serialize({"scan_id": scan_id})

    async def get_scan_status(scan_id: str) -> str:
        rows = client._query("code_atlas_scans", "id=?", (scan_id,))  # noqa: SLF001
        return _serialize(rows[0] if rows else None)

    async def add_node(
        node_type: str,
        name: str,
        description: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        created = await client.add_nodes(
            [
                {
                    "project_id": config.project_id or None,
                    "node_type": node_type,
                    "name": name,
                    "description": description or "",
                    "metadata": metadata or {},
                }
            ]
        )
        return _serialize(created[0])

    async def add_edge(
        from_node_id: str,
        to_node_id: str,
        edge_type: str,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        created = await client.add_edges(
            [
                {
                    "project_id": config.project_id or None,
                    "from_node_id": from_node_id,
                    "to_node_id": to_node_id,
                    "edge_type": edge_type,
                    "metadata": metadata or {},
                }
            ]
        )
        return _serialize(created[0])

    async def query_graph() -> str:
        return _serialize(client.query_graph())

    async def get_performance_summary(limit: int = 50) -> str:
        return _serialize(client.get_performance_summary(limit))

    async def sync_state() -> str:
        # Force flush: in this implementation everything is synced eagerly,
        # but the hook exists for future buffer-and-flush behaviour.
        return _serialize({"ok": True, "mode": client.mode})

    return {
        "get_tasks": get_tasks,
        "create_task": create_task,
        "update_task": update_task,
        "delete_task": delete_task,
        "log_blocker": log_blocker,
        "resolve_blocker": resolve_blocker,
        "log_decision": log_decision,
        "log_work": log_work,
        "start_session": start_session,
        "end_session": end_session,
        "scan_codebase": scan_codebase,
        "get_scan_status": get_scan_status,
        "add_node": add_node,
        "add_edge": add_edge,
        "query_graph": query_graph,
        "get_performance_summary": get_performance_summary,
        "sync_state": sync_state,
    }


async def run_stdio() -> None:
    """Run the MCP tool server over stdio."""
    from mcp.server.fastmcp import FastMCP  # imported lazily to avoid hard dep in tests

    config = load_config()
    client = BackendClient(config)
    await client.register()

    server = FastMCP("obsmcp")
    handlers = await _build_tool_handlers(client, config)

    for name, fn in handlers.items():
        server.tool(name=name)(fn)

    logger.info("OBSMCP MCP server starting (mode=%s)", config.mode)
    await server.run_stdio_async()


def main() -> None:
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(run_stdio())


if __name__ == "__main__":  # pragma: no cover
    main()
