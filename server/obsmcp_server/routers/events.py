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
def stats() -> dict[str, Any]:
    db = get_db()

    def scalar(sql: str, params: tuple = ()) -> int:
        row = db.execute(sql, params).fetchone()
        return int(row[0]) if row else 0

    return {
        "tasks": {
            "total": scalar("SELECT COUNT(*) FROM tasks"),
            "open": scalar("SELECT COUNT(*) FROM tasks WHERE status='open'"),
            "in_progress": scalar("SELECT COUNT(*) FROM tasks WHERE status='in_progress'"),
            "blocked": scalar("SELECT COUNT(*) FROM tasks WHERE status='blocked'"),
            "done": scalar("SELECT COUNT(*) FROM tasks WHERE status='done'"),
        },
        "sessions": {
            "total": scalar("SELECT COUNT(*) FROM sessions"),
            "active": scalar("SELECT COUNT(*) FROM sessions WHERE ended_at IS NULL"),
        },
        "blockers": {
            "active": scalar("SELECT COUNT(*) FROM blockers WHERE status='active'"),
            "resolved": scalar("SELECT COUNT(*) FROM blockers WHERE status='resolved'"),
        },
        "decisions": scalar("SELECT COUNT(*) FROM decisions"),
        "work_logs": scalar("SELECT COUNT(*) FROM work_logs"),
        "nodes": scalar("SELECT COUNT(*) FROM knowledge_nodes"),
        "edges": scalar("SELECT COUNT(*) FROM knowledge_edges"),
        "agents": scalar("SELECT COUNT(*) FROM agent_configs"),
    }
