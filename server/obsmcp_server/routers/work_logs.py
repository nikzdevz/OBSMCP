"""Work log endpoints."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel

from ..sse import broadcast_event
from ._helpers import delete_row, get_row, insert_row, list_rows, new_id, now_iso, update_row

router = APIRouter()


class WorkLogCreate(BaseModel):
    id: str | None = None
    project_id: str | None = None
    session_id: str | None = None
    agent_id: str | None = None
    description: str
    hours: float | None = None
    tags: list[str] | None = None


class WorkLogUpdate(BaseModel):
    description: str | None = None
    hours: float | None = None
    tags: list[str] | None = None


@router.post("")
def log_work(body: WorkLogCreate) -> dict[str, Any]:
    data = {
        "id": body.id or new_id(),
        **body.model_dump(exclude_unset=False, exclude={"id"}),
        "created_at": now_iso(),
    }
    row = insert_row("work_logs", data, json_columns=("tags",))
    broadcast_event("work_logged", row)
    return row


@router.get("")
def list_work_logs(
    session_id: str | None = None,
    project_id: str | None = None,
    date: str | None = None,
) -> list[dict[str, Any]]:
    wheres: list[str] = []
    params: list[Any] = []
    if session_id:
        wheres.append("session_id=?")
        params.append(session_id)
    if project_id:
        wheres.append("project_id=?")
        params.append(project_id)
    if date:
        wheres.append("date(created_at)=date(?)")
        params.append(date)
    return list_rows("work_logs", " AND ".join(wheres), tuple(params))


@router.put("/{log_id}")
def update_work_log(log_id: str, body: WorkLogUpdate) -> dict[str, Any]:
    row = update_row("work_logs", log_id, body.model_dump(exclude_unset=True), json_columns=("tags",))
    broadcast_event("work_log_updated", row)
    return row


@router.delete("/{log_id}")
def delete_work_log(log_id: str) -> dict[str, Any]:
    delete_row("work_logs", log_id)
    broadcast_event("work_log_deleted", {"id": log_id})
    return {"ok": True}


@router.get("/{log_id}")
def get_work_log(log_id: str) -> dict[str, Any]:
    return get_row("work_logs", log_id)
