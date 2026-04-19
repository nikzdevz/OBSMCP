"""Project endpoints."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel

from ._helpers import delete_row, get_row, insert_row, list_rows, new_id, now_iso, update_row

router = APIRouter()


class ProjectCreate(BaseModel):
    id: str | None = None
    name: str
    path: str
    repo_url: str | None = None


class ProjectUpdate(BaseModel):
    name: str | None = None
    path: str | None = None
    repo_url: str | None = None


@router.post("")
def create_project(body: ProjectCreate) -> dict[str, Any]:
    now = now_iso()
    data = {
        "id": body.id or new_id(),
        "name": body.name,
        "path": body.path,
        "repo_url": body.repo_url,
        "created_at": now,
        "updated_at": now,
    }
    return insert_row("projects", data)


@router.get("")
def list_projects() -> list[dict[str, Any]]:
    return list_rows("projects")


@router.get("/{project_id}")
def get_project(project_id: str) -> dict[str, Any]:
    return get_row("projects", project_id)


@router.put("/{project_id}")
def update_project(project_id: str, body: ProjectUpdate) -> dict[str, Any]:
    updates = body.model_dump(exclude_unset=True)
    updates["updated_at"] = now_iso()
    return update_row("projects", project_id, updates)


@router.delete("/{project_id}")
def delete_project(project_id: str) -> dict[str, Any]:
    delete_row("projects", project_id)
    return {"ok": True}
