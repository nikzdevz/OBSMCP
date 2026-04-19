"""Agent registration and heartbeat endpoints."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel

from ..db import get_db, row_to_dict, rows_to_list
from ..sse import broadcast_event
from ._helpers import now_iso

router = APIRouter()


class AgentRegister(BaseModel):
    agent_id: str
    project_id: str | None = None
    machine_name: str | None = None
    os_type: str | None = None
    display_name: str | None = None


@router.post("/register")
def register(body: AgentRegister) -> dict[str, Any]:
    db = get_db()
    db.execute(
        """INSERT INTO agent_configs(agent_id, project_id, machine_name, os_type, display_name, last_seen_at, created_at)
           VALUES(?,?,?,?,?,?,?)
           ON CONFLICT(agent_id) DO UPDATE SET
             project_id=excluded.project_id,
             machine_name=excluded.machine_name,
             os_type=excluded.os_type,
             display_name=excluded.display_name,
             last_seen_at=excluded.last_seen_at""",
        (
            body.agent_id,
            body.project_id,
            body.machine_name,
            body.os_type,
            body.display_name,
            now_iso(),
            now_iso(),
        ),
    )
    row = db.execute("SELECT * FROM agent_configs WHERE agent_id=?", (body.agent_id,)).fetchone()
    broadcast_event("agent_connected", row_to_dict(row) or {})
    return row_to_dict(row) or {}


@router.put("/{agent_id}/heartbeat")
def heartbeat(agent_id: str) -> dict[str, Any]:
    db = get_db()
    db.execute(
        "UPDATE agent_configs SET last_seen_at=? WHERE agent_id=?", (now_iso(), agent_id)
    )
    return {"ok": True}


@router.get("")
def list_agents() -> list[dict[str, Any]]:
    db = get_db()
    rows = db.execute("SELECT * FROM agent_configs ORDER BY last_seen_at DESC").fetchall()
    return rows_to_list(rows)
