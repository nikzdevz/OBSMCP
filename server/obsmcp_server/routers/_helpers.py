"""Shared helpers for CRUD routers."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import Any

from fastapi import HTTPException

from ..db import encode_json_columns, get_db, row_to_dict, rows_to_list


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


def new_id() -> str:
    return str(uuid.uuid4())


def insert_row(
    table: str,
    data: dict[str, Any],
    json_columns: tuple[str, ...] = (),
) -> dict[str, Any]:
    data = encode_json_columns(data, json_columns)
    cols = list(data.keys())
    placeholders = ",".join(["?"] * len(cols))
    sql = f"INSERT INTO {table} ({','.join(cols)}) VALUES ({placeholders})"
    db = get_db()
    db.execute(sql, list(data.values()))
    return get_row(table, data["id"])


def update_row(
    table: str,
    row_id: str,
    updates: dict[str, Any],
    json_columns: tuple[str, ...] = (),
) -> dict[str, Any]:
    if not updates:
        return get_row(table, row_id)
    updates = encode_json_columns(updates, json_columns)
    sets = ",".join(f"{k}=?" for k in updates)
    sql = f"UPDATE {table} SET {sets} WHERE id=?"
    db = get_db()
    cur = db.execute(sql, [*updates.values(), row_id])
    if cur.rowcount == 0:
        raise HTTPException(status_code=404, detail=f"{table[:-1]} not found")
    return get_row(table, row_id)


def delete_row(table: str, row_id: str) -> None:
    db = get_db()
    cur = db.execute(f"DELETE FROM {table} WHERE id=?", (row_id,))
    if cur.rowcount == 0:
        raise HTTPException(status_code=404, detail=f"{table[:-1]} not found")


def get_row(table: str, row_id: str) -> dict[str, Any]:
    db = get_db()
    row = db.execute(f"SELECT * FROM {table} WHERE id=?", (row_id,)).fetchone()
    result = row_to_dict(row)
    if result is None:
        raise HTTPException(status_code=404, detail=f"{table[:-1]} not found")
    return result


def list_rows(table: str, where: str = "", params: tuple = ()) -> list[dict[str, Any]]:
    sql = f"SELECT * FROM {table}"
    if where:
        sql += f" WHERE {where}"
    sql += " ORDER BY created_at DESC" if _has_created_at(table) else ""
    db = get_db()
    rows = db.execute(sql, params).fetchall()
    return rows_to_list(rows)


def _has_created_at(table: str) -> bool:
    return table not in {"agent_configs", "code_atlas_files", "performance_logs", "sessions"}


def coerce_tags(value: Any) -> Any:
    """Accept a list from JSON bodies; serialize to string when writing."""
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    return value
