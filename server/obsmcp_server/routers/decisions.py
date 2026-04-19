"""Decision endpoints."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel

from ..sse import broadcast_event
from ._helpers import delete_row, get_row, insert_row, list_rows, new_id, now_iso, update_row

router = APIRouter()


class DecisionCreate(BaseModel):
    id: str | None = None
    project_id: str | None = None
    agent_id: str | None = None
    decision: str
    context: str | None = None
    outcome: str | None = None
    tags: list[str] | None = None


class DecisionUpdate(BaseModel):
    decision: str | None = None
    context: str | None = None
    outcome: str | None = None
    tags: list[str] | None = None


@router.post("")
def log_decision(body: DecisionCreate) -> dict[str, Any]:
    data = {
        "id": body.id or new_id(),
        **body.model_dump(exclude_unset=False, exclude={"id"}),
        "created_at": now_iso(),
    }
    row = insert_row("decisions", data, json_columns=("tags",))
    broadcast_event("decision_logged", row)
    return row


@router.get("")
def list_decisions(project_id: str | None = None) -> list[dict[str, Any]]:
    if project_id:
        return list_rows("decisions", "project_id=?", (project_id,))
    return list_rows("decisions")


@router.get("/{decision_id}")
def get_decision(decision_id: str) -> dict[str, Any]:
    return get_row("decisions", decision_id)


@router.put("/{decision_id}")
def update_decision(decision_id: str, body: DecisionUpdate) -> dict[str, Any]:
    updates = body.model_dump(exclude_unset=True)
    row = update_row("decisions", decision_id, updates, json_columns=("tags",))
    broadcast_event("decision_updated", row)
    return row


@router.delete("/{decision_id}")
def delete_decision(decision_id: str) -> dict[str, Any]:
    delete_row("decisions", decision_id)
    broadcast_event("decision_deleted", {"id": decision_id})
    return {"ok": True}
