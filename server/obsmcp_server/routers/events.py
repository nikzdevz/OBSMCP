"""SSE events + stats endpoints."""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from ..db import get_db
from ..sse import format_sse, register_listener, unregister_listener

router = APIRouter()


@router.get("/events")
async def sse_events(request: Request) -> StreamingResponse:
    queue = await register_listener()

    async def event_stream():
        try:
            # Initial "connected" event
            yield format_sse({"type": "connected", "payload": {}, "timestamp": ""})
            while True:
                if await request.is_disconnected():
                    break
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15.0)
                    yield format_sse(event)
                except TimeoutError:
                    # Heartbeat to keep connection alive
                    yield ": heartbeat\n\n"
        finally:
            await unregister_listener(queue)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/stats")
def stats(project_id: str | None = None) -> dict[str, Any]:
    db = get_db()
    base_where = "WHERE 1=1" if project_id is None else "WHERE project_id=?"
    args: tuple[Any, ...] = () if project_id is None else (project_id,)

    def scalar(sql: str, params: tuple[Any, ...] = ()) -> int:
        row = db.execute(sql, params).fetchone()
        return int(row[0]) if row else 0

    def count(table: str, extra: str = "") -> int:
        sql = f"SELECT COUNT(*) FROM {table} {base_where}{extra}"
        return scalar(sql, args)

    return {
        "tasks": {
            "total": count("tasks"),
            "open": count("tasks", " AND status='open'"),
            "in_progress": count("tasks", " AND status='in_progress'"),
            "blocked": count("tasks", " AND status='blocked'"),
            "done": count("tasks", " AND status='done'"),
        },
        "sessions": {
            "total": count("sessions"),
            "active": count("sessions", " AND ended_at IS NULL"),
        },
        "blockers": {
            "active": count("blockers", " AND status='active'"),
            "resolved": count("blockers", " AND status='resolved'"),
        },
        "decisions": count("decisions"),
        "work_logs": count("work_logs"),
        "nodes": count("knowledge_nodes"),
        "edges": count("knowledge_edges"),
        "agents": scalar("SELECT COUNT(*) FROM agent_configs"),
    }
