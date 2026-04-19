"""Session endpoints."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel

from ..db import get_db
from ..sse import broadcast_event
from ._helpers import get_row, insert_row, list_rows, new_id, now_iso, update_row

router = APIRouter()


class SessionCreate(BaseModel):
    id: str | None = None
    project_id: str | None = None
    agent_id: str
    context: str | None = None


class SessionHeartbeat(BaseModel):
    context: str | None = None


@router.post("")
def open_session(body: SessionCreate) -> dict[str, Any]:
    data = {
        "id": body.id or new_id(),
        "project_id": body.project_id,
        "agent_id": body.agent_id,
        "started_at": now_iso(),
        "ended_at": None,
        "duration_seconds": None,
        "context": body.context,
    }
    row = insert_row("sessions", data)
    broadcast_event("session_opened", row)
    return row


@router.get("")
def list_sessions(active: bool | None = None, project_id: str | None = None) -> list[dict[str, Any]]:
    wheres: list[str] = []
    params: list[Any] = []
    if active is True:
        wheres.append("ended_at IS NULL")
    elif active is False:
        wheres.append("ended_at IS NOT NULL")
    if project_id:
        wheres.append("project_id=?")
        params.append(project_id)
    return list_rows("sessions", " AND ".join(wheres), tuple(params))


@router.get("/{session_id}")
def get_session(session_id: str) -> dict[str, Any]:
    return get_row("sessions", session_id)


@router.put("/{session_id}/heartbeat")
def heartbeat(session_id: str, body: SessionHeartbeat) -> dict[str, Any]:
    updates: dict[str, Any] = {}
    if body.context is not None:
        updates["context"] = body.context
    row = update_row("sessions", session_id, updates) if updates else get_row("sessions", session_id)
    broadcast_event("session_heartbeat", {"id": session_id, "context": body.context})
    return row


@router.put("/{session_id}/close")
def close_session(session_id: str) -> dict[str, Any]:
    session = get_row("sessions", session_id)
    started_at = session.get("started_at")
    ended_at = now_iso()
    duration = 0
    if started_at:
        try:
            started = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
            if started.tzinfo is None:
                started = started.replace(tzinfo=UTC)
            duration = int((datetime.now(UTC) - started).total_seconds())
        except ValueError:
            duration = 0
    db = get_db()
    db.execute(
        "UPDATE sessions SET ended_at=?, duration_seconds=? WHERE id=?",
        (ended_at, duration, session_id),
    )
    row = get_row("sessions", session_id)
    broadcast_event("session_closed", row)
    return row
