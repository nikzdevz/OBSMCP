"""Task CRUD endpoints."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from ..sse import broadcast_event
from ._helpers import (
    delete_row,
    get_row,
    insert_row,
    list_rows,
    new_id,
    now_iso,
    update_row,
)

router = APIRouter()


class TaskCreate(BaseModel):
    id: str | None = None
    project_id: str | None = None
    title: str
    description: str | None = None
    status: str = "open"
    priority: str = "medium"
    tags: list[str] | None = None


class TaskUpdate(BaseModel):
    project_id: str | None = None
    title: str | None = None
    description: str | None = None
    status: str | None = None
    priority: str | None = None
    tags: list[str] | None = None


class BulkOperation(BaseModel):
    id: str
    action: str = Field(pattern="^(update|delete)$")
    data: dict[str, Any] | None = None


class BulkRequest(BaseModel):
    operations: list[BulkOperation]


@router.post("")
def create_task(body: TaskCreate) -> dict[str, Any]:
    now = now_iso()
    data = {
        "id": body.id or new_id(),
        "project_id": body.project_id,
        "title": body.title,
        "description": body.description,
        "status": body.status,
        "priority": body.priority,
        "tags": body.tags,
        "created_at": now,
        "updated_at": now,
    }
    row = insert_row("tasks", data, json_columns=("tags",))
    broadcast_event("task_created", row)
    return row


@router.get("")
def list_tasks(
    status: str | None = None,
    project_id: str | None = None,
) -> list[dict[str, Any]]:
    wheres: list[str] = []
    params: list[Any] = []
    if status:
        wheres.append("status=?")
        params.append(status)
    if project_id:
        wheres.append("project_id=?")
        params.append(project_id)
    return list_rows("tasks", " AND ".join(wheres), tuple(params))


@router.get("/{task_id}")
def get_task(task_id: str) -> dict[str, Any]:
    return get_row("tasks", task_id)


@router.put("/{task_id}")
def update_task(task_id: str, body: TaskUpdate) -> dict[str, Any]:
    updates = dict(body.model_dump(exclude_unset=True).items())
    updates["updated_at"] = now_iso()
    row = update_row("tasks", task_id, updates, json_columns=("tags",))
    broadcast_event("task_updated", row)
    return row


@router.delete("/{task_id}")
def delete_task(task_id: str) -> dict[str, Any]:
    delete_row("tasks", task_id)
    broadcast_event("task_deleted", {"id": task_id})
    return {"ok": True}


@router.post("/bulk")
def bulk_tasks(body: BulkRequest) -> dict[str, Any]:
    results: list[dict[str, Any]] = []
    for op in body.operations:
        if op.action == "update":
            data = op.data or {}
            data["updated_at"] = now_iso()
            row = update_row("tasks", op.id, data, json_columns=("tags",))
            broadcast_event("task_updated", row)
            results.append(row)
        elif op.action == "delete":
            delete_row("tasks", op.id)
            broadcast_event("task_deleted", {"id": op.id})
            results.append({"id": op.id, "deleted": True})
        else:  # pragma: no cover
            raise HTTPException(status_code=400, detail=f"unknown action: {op.action}")
    return {"results": results}
