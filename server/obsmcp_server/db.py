"""SQLite connection management + schema migrations.

Raw sqlite3 by design — no ORM. Each thread gets its own connection via
``threading.local`` to avoid cross-thread issues.
"""

from __future__ import annotations

import contextlib
import json
import sqlite3
import threading
from pathlib import Path
from typing import Any

_SCHEMA_FILE = Path(__file__).parent / "schema.sql"

_state: dict[str, Any] = {"db_path": None}
_tls = threading.local()


def init_db(db_path: str) -> None:
    """Initialize the DB file and run migrations. Idempotent."""
    _state["db_path"] = db_path
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = get_connection()
    run_migrations(conn)


def get_connection() -> sqlite3.Connection:
    """Return a thread-local SQLite connection."""
    conn = getattr(_tls, "conn", None)
    if conn is None:
        db_path = _state.get("db_path")
        if not db_path:
            raise RuntimeError("DB not initialized; call init_db() first")
        conn = sqlite3.connect(db_path, check_same_thread=False, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
        _tls.conn = conn
    return conn


def get_db() -> sqlite3.Connection:
    return get_connection()


def run_migrations(conn: sqlite3.Connection) -> None:
    sql = _SCHEMA_FILE.read_text(encoding="utf-8")
    cur = conn.cursor()
    cur.executescript(sql)
    cur.execute(
        "INSERT OR REPLACE INTO schema_meta(key, value) VALUES('version', '1')"
    )


def row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    d = dict(row)
    for key in ("tags", "imports", "exports", "metadata"):
        if key in d and isinstance(d[key], str) and d[key]:
            with contextlib.suppress(json.JSONDecodeError):
                d[key] = json.loads(d[key])
    return d


def rows_to_list(rows: list[sqlite3.Row]) -> list[dict[str, Any]]:
    return [row_to_dict(r) or {} for r in rows]


def encode_json_columns(data: dict[str, Any], columns: tuple[str, ...]) -> dict[str, Any]:
    out = dict(data)
    for col in columns:
        if col in out and not isinstance(out[col], str) and out[col] is not None:
            out[col] = json.dumps(out[col])
    return out
