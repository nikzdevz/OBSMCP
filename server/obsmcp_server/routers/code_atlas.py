"""Code Atlas scan endpoints."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel

from ..db import get_db, rows_to_list
from ..sse import broadcast_event
from ._helpers import get_row, insert_row, list_rows, new_id, now_iso, update_row

router = APIRouter()


class ScanCreate(BaseModel):
    project_id: str | None = None
    agent_id: str | None = None
    force_refresh: bool = False


class FileCreate(BaseModel):
    scan_id: str
    project_id: str | None = None
    file_path: str
    language: str | None = None
    functions_count: int = 0
    imports: list[str] | None = None
    exports: list[str] | None = None
    semantic_description: str | None = None


class FileBatch(BaseModel):
    files: list[FileCreate]


class ScanProgress(BaseModel):
    status: str | None = None
    total_files: int | None = None
    scanned_files: int | None = None
    error_message: str | None = None


@router.post("/scan")
def start_scan(body: ScanCreate) -> dict[str, Any]:
    data = {
        "id": new_id(),
        "project_id": body.project_id,
        "agent_id": body.agent_id,
        "status": "pending",
        "total_files": 0,
        "scanned_files": 0,
        "started_at": now_iso(),
    }
    row = insert_row("code_atlas_scans", data)
    broadcast_event("scan_started", row)
    return row


@router.get("/scan/{scan_id}")
def get_scan(scan_id: str) -> dict[str, Any]:
    return get_row("code_atlas_scans", scan_id)


@router.put("/scan/{scan_id}")
def update_scan(scan_id: str, body: ScanProgress) -> dict[str, Any]:
    updates = body.model_dump(exclude_unset=True)
    if body.status == "completed":
        updates["completed_at"] = now_iso()
    row = update_row("code_atlas_scans", scan_id, updates)
    broadcast_event(f"scan_{body.status or 'progress'}", row)
    return row


@router.get("/scan/{scan_id}/files")
def get_scan_files(
    scan_id: str,
    page: int = 1,
    per_page: int = 100,
) -> dict[str, Any]:
    db = get_db()
    total = db.execute(
        "SELECT COUNT(*) FROM code_atlas_files WHERE scan_id=?", (scan_id,)
    ).fetchone()[0]
    rows = db.execute(
        "SELECT * FROM code_atlas_files WHERE scan_id=? ORDER BY file_path LIMIT ? OFFSET ?",
        (scan_id, per_page, (page - 1) * per_page),
    ).fetchall()
    return {"total": total, "page": page, "per_page": per_page, "files": rows_to_list(rows)}


@router.post("/files")
def add_file(body: FileCreate) -> dict[str, Any]:
    data = {
        "id": new_id(),
        **body.model_dump(),
        "scanned_at": now_iso(),
    }
    row = insert_row("code_atlas_files", data, json_columns=("imports", "exports"))
    return row


@router.post("/files/bulk")
def add_files_bulk(body: FileBatch) -> dict[str, Any]:
    results = []
    for f in body.files:
        data = {
            "id": new_id(),
            **f.model_dump(),
            "scanned_at": now_iso(),
        }
        results.append(insert_row("code_atlas_files", data, json_columns=("imports", "exports")))
    return {"count": len(results)}


@router.get("")
def list_scans(project_id: str | None = None) -> list[dict[str, Any]]:
    if project_id:
        return list_rows("code_atlas_scans", "project_id=?", (project_id,))
    return list_rows("code_atlas_scans")
