"""Performance log endpoints."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel

from ..db import get_db, rows_to_list
from ..sse import broadcast_event
from ._helpers import insert_row, new_id, now_iso

router = APIRouter()


class PerfLog(BaseModel):
    id: str | None = None
    project_id: str | None = None
    agent_id: str | None = None
    session_id: str | None = None
    metric_name: str
    metric_value: float
    unit: str | None = None
    tags: dict[str, Any] | None = None


class PerfLogBatch(BaseModel):
    logs: list[PerfLog]


@router.post("")
def ingest_logs(body: PerfLogBatch) -> dict[str, Any]:
    rows = []
    for log in body.logs:
        data = {
            "id": log.id or new_id(),
            **log.model_dump(exclude_unset=False, exclude={"id"}),
            "logged_at": now_iso(),
        }
        rows.append(insert_row("performance_logs", data, json_columns=("tags",)))
    broadcast_event("perf_log_received", {"count": len(rows)})
    return {"count": len(rows)}


@router.get("")
def list_logs(
    metric_name: str | None = None,
    session_id: str | None = None,
    project_id: str | None = None,
    from_: str | None = None,
    to: str | None = None,
    limit: int = 1000,
) -> list[dict[str, Any]]:
    wheres: list[str] = []
    params: list[Any] = []
    if metric_name:
        wheres.append("metric_name=?")
        params.append(metric_name)
    if session_id:
        wheres.append("session_id=?")
        params.append(session_id)
    if project_id:
        wheres.append("project_id=?")
        params.append(project_id)
    if from_:
        wheres.append("logged_at>=?")
        params.append(from_)
    if to:
        wheres.append("logged_at<=?")
        params.append(to)
    sql = "SELECT * FROM performance_logs"
    if wheres:
        sql += " WHERE " + " AND ".join(wheres)
    sql += " ORDER BY logged_at DESC LIMIT ?"
    params.append(limit)
    rows = get_db().execute(sql, tuple(params)).fetchall()
    return rows_to_list(rows)


@router.get("/summary")
def summary(
    metric_name: str | None = None,
    session_id: str | None = None,
    from_: str | None = None,
    to: str | None = None,
) -> dict[str, Any]:
    wheres: list[str] = []
    params: list[Any] = []
    if metric_name:
        wheres.append("metric_name=?")
        params.append(metric_name)
    if session_id:
        wheres.append("session_id=?")
        params.append(session_id)
    if from_:
        wheres.append("logged_at>=?")
        params.append(from_)
    if to:
        wheres.append("logged_at<=?")
        params.append(to)
    where_sql = (" WHERE " + " AND ".join(wheres)) if wheres else ""
    db = get_db()
    rows = db.execute(
        f"""
        SELECT metric_name,
               COUNT(*) as count,
               AVG(metric_value) as avg,
               MIN(metric_value) as min,
               MAX(metric_value) as max
        FROM performance_logs
        {where_sql}
        GROUP BY metric_name
        """,
        tuple(params),
    ).fetchall()
    return {"metrics": rows_to_list(rows)}
