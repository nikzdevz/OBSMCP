"""Blocker endpoints."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel

from ..sse import broadcast_event
from ._helpers import delete_row, get_row, insert_row, list_rows, new_id, now_iso, update_row

router = APIRouter()


class BlockerCreate(BaseModel):
    id: str | None = None
    project_id: str | None = None
    agent_id: str | None = None
    description: str
    severity: str = "medium"


class BlockerResolve(BaseModel):
    resolution: str


@router.post("")
def log_blocker(body: BlockerCreate) -> dict[str, Any]:
    data = {
        "id": body.id or new_id(),
        "project_id": body.project_id,
        "agent_id": body.agent_id,
        "description": body.description,
        "severity": body.severity,
        "status": "active",
        "created_at": now_iso(),
    }
    row = insert_row("blockers", data)
    broadcast_event("blocker_logged", row)
    return row


@router.get("")
def list_blockers(status: str | None = None, project_id: str | None = None) -> list[dict[str, Any]]:
    wheres: list[str] = []
    params: list[Any] = []
    if status:
        wheres.append("status=?")
        params.append(status)
    if project_id:
        wheres.append("project_id=?")
        params.append(project_id)
    return list_rows("blockers", " AND ".join(wheres), tuple(params))


@router.get("/{blocker_id}")
def get_blocker(blocker_id: str) -> dict[str, Any]:
    return get_row("blockers", blocker_id)


@router.put("/{blocker_id}/resolve")
def resolve_blocker(blocker_id: str, body: BlockerResolve) -> dict[str, Any]:
    row = update_row(
        "blockers",
        blocker_id,
        {
            "status": "resolved",
            "resolved_at": now_iso(),
            "resolution": body.resolution,
        },
    )
    broadcast_event("blocker_resolved", row)
    return row


@router.delete("/{blocker_id}")
def delete_blocker(blocker_id: str) -> dict[str, Any]:
    delete_row("blockers", blocker_id)
    broadcast_event("blocker_deleted", {"id": blocker_id})
    return {"ok": True}
