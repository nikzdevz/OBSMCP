"""Dual-mode HTTP client.

- Always writes to the local SQLite DB first (offline-first).
- If ``backend_url`` is configured, also pushes the same write to the remote
  server in the background. Cloud failures never fail the local write.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import platform
import sqlite3
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from ..config import Config
from ..utils.logger import get_logger

logger = get_logger("obsmcp.client")

_SCHEMA = (Path(__file__).resolve().parents[3] / "server" / "obsmcp_server" / "schema.sql")


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _json_dump(value: Any) -> Any:
    if isinstance(value, (list, dict)):
        return json.dumps(value)
    return value


class BackendClient:
    """Local-first client with optional cloud sync."""

    _JSON_COLUMNS: dict[str, tuple[str, ...]] = {
        "tasks": ("tags",),
        "decisions": ("tags",),
        "work_logs": ("tags",),
        "code_atlas_files": ("imports", "exports"),
        "knowledge_nodes": ("metadata",),
        "knowledge_edges": ("metadata",),
        "performance_logs": ("tags",),
    }

    def __init__(self, config: Config) -> None:
        self.config = config
        self.agent_id = config.agent_id
        self.mode = config.mode
        self.base_url = config.backend_url.rstrip("/")
        self.token = config.api_token
        self.db_path = config.local_db_path

        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._ensure_tables()
        self._lock = asyncio.Lock()

        self._http: httpx.AsyncClient | None = None
        if self.mode == "cloud":
            headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
            self._http = httpx.AsyncClient(
                base_url=self.base_url,
                headers=headers,
                timeout=10.0,
            )

    # ---------------------------------------------------------------- schema
    def _ensure_tables(self) -> None:
        if not _SCHEMA.exists():
            logger.warning("schema.sql not found at %s; skipping local init", _SCHEMA)
            return
        self._conn.executescript(_SCHEMA.read_text(encoding="utf-8"))

    # ---------------------------------------------------------------- local
    def _encode(self, table: str, data: dict[str, Any]) -> dict[str, Any]:
        out = dict(data)
        for col in self._JSON_COLUMNS.get(table, ()):  # pragma: no cover - simple loop
            if col in out and out[col] is not None and not isinstance(out[col], str):
                out[col] = json.dumps(out[col])
        return out

    def _write(self, table: str, data: dict[str, Any]) -> dict[str, Any]:
        encoded = self._encode(table, data)
        cols = list(encoded.keys())
        placeholders = ",".join(["?"] * len(cols))
        sql = f"INSERT OR REPLACE INTO {table} ({','.join(cols)}) VALUES ({placeholders})"
        self._conn.execute(sql, list(encoded.values()))
        return data

    def _query(self, table: str, where: str = "", params: tuple = ()) -> list[dict[str, Any]]:
        sql = f"SELECT * FROM {table}"
        if where:
            sql += f" WHERE {where}"
        cur = self._conn.execute(sql, params)
        return [dict(r) for r in cur.fetchall()]

    # ---------------------------------------------------------------- cloud
    async def _cloud_post(self, path: str, body: Any) -> None:
        if self.mode != "cloud" or self._http is None:
            return
        try:
            await self._http.post(path, json=body)
        except Exception as exc:  # noqa: BLE001
            logger.warning("cloud sync failed on POST %s: %s", path, exc)

    async def _cloud_put(self, path: str, body: Any) -> None:
        if self.mode != "cloud" or self._http is None:
            return
        try:
            await self._http.put(path, json=body)
        except Exception as exc:  # noqa: BLE001
            logger.warning("cloud sync failed on PUT %s: %s", path, exc)

    # ------------------------------------------------------------- lifecycle
    async def register(self) -> dict[str, Any]:
        data = {
            "agent_id": self.agent_id,
            "project_id": self.config.project_id or None,
            "machine_name": platform.node(),
            "os_type": platform.system(),
            "display_name": f"{platform.node()} ({platform.system()})",
            "last_seen_at": _now(),
            "created_at": _now(),
        }
        self._write("agent_configs", data)
        if self.mode == "cloud":
            asyncio.create_task(self._cloud_post("/api/agents/register", data))
        return data

    async def heartbeat(self) -> None:
        self._conn.execute(
            "UPDATE agent_configs SET last_seen_at=? WHERE agent_id=?",
            (_now(), self.agent_id),
        )
        if self.mode == "cloud":
            asyncio.create_task(self._cloud_put(f"/api/agents/{self.agent_id}/heartbeat", {}))

    async def close(self) -> None:
        if self._http is not None:
            await self._http.aclose()
        self._conn.close()

    # ----------------------------------------------------------------- tasks
    async def create_task(self, task: dict[str, Any]) -> dict[str, Any]:
        now = _now()
        task.setdefault("id", str(uuid.uuid4()))
        task.setdefault("status", "open")
        task.setdefault("priority", "medium")
        task["created_at"] = now
        task["updated_at"] = now
        self._write("tasks", task)
        if self.mode == "cloud":
            asyncio.create_task(self._cloud_post("/api/tasks", task))
        return task

    async def update_task(self, task_id: str, updates: dict[str, Any]) -> dict[str, Any]:
        updates["updated_at"] = _now()
        existing = self._query("tasks", "id=?", (task_id,))
        if not existing:
            raise KeyError(f"task {task_id} not found")
        merged = {**existing[0], **updates, "id": task_id}
        self._write("tasks", merged)
        if self.mode == "cloud":
            asyncio.create_task(self._cloud_put(f"/api/tasks/{task_id}", updates))
        return merged

    async def delete_task(self, task_id: str) -> None:
        self._conn.execute("DELETE FROM tasks WHERE id=?", (task_id,))
        if self.mode == "cloud" and self._http is not None:
            with contextlib.suppress(Exception):
                await self._http.delete(f"/api/tasks/{task_id}")

    def list_tasks(self, status: str | None = None) -> list[dict[str, Any]]:
        if status:
            return self._query("tasks", "status=?", (status,))
        return self._query("tasks")

    # -------------------------------------------------------------- sessions
    async def start_session(self, project_id: str, context: str = "") -> dict[str, Any]:
        session = {
            "id": str(uuid.uuid4()),
            "project_id": project_id,
            "agent_id": self.agent_id,
            "started_at": _now(),
            "ended_at": None,
            "duration_seconds": None,
            "context": context,
        }
        self._write("sessions", session)
        if self.mode == "cloud":
            asyncio.create_task(self._cloud_post("/api/sessions", session))
        return session

    async def end_session(self, session_id: str) -> None:
        rows = self._query("sessions", "id=?", (session_id,))
        if not rows:
            return
        session = rows[0]
        started = session.get("started_at")
        duration = 0
        if started:
            try:
                ts = datetime.fromisoformat(started.replace("Z", "+00:00"))
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=UTC)
                duration = int((datetime.now(UTC) - ts).total_seconds())
            except ValueError:
                pass
        self._conn.execute(
            "UPDATE sessions SET ended_at=?, duration_seconds=? WHERE id=?",
            (_now(), duration, session_id),
        )
        if self.mode == "cloud":
            asyncio.create_task(self._cloud_put(f"/api/sessions/{session_id}/close", {}))

    async def heartbeat_session(self, session_id: str, context: str = "") -> None:
        # Local-only heartbeat — don't spam the server.
        self._conn.execute(
            "UPDATE sessions SET context=COALESCE(?, context) WHERE id=?",
            (context or None, session_id),
        )

    # -------------------------------------------------------------- blockers
    async def log_blocker(self, blocker: dict[str, Any]) -> dict[str, Any]:
        blocker.setdefault("id", str(uuid.uuid4()))
        blocker.setdefault("status", "active")
        blocker.setdefault("severity", "medium")
        blocker["agent_id"] = self.agent_id
        blocker["created_at"] = _now()
        self._write("blockers", blocker)
        if self.mode == "cloud":
            asyncio.create_task(self._cloud_post("/api/blockers", blocker))
        return blocker

    async def resolve_blocker(self, blocker_id: str, resolution: str) -> dict[str, Any]:
        rows = self._query("blockers", "id=?", (blocker_id,))
        if not rows:
            raise KeyError(blocker_id)
        merged = {**rows[0], "status": "resolved", "resolved_at": _now(), "resolution": resolution}
        self._write("blockers", merged)
        if self.mode == "cloud":
            asyncio.create_task(
                self._cloud_put(f"/api/blockers/{blocker_id}/resolve", {"resolution": resolution})
            )
        return merged

    # ------------------------------------------------------------- decisions
    async def log_decision(self, decision: dict[str, Any]) -> dict[str, Any]:
        decision.setdefault("id", str(uuid.uuid4()))
        decision["agent_id"] = self.agent_id
        decision["created_at"] = _now()
        self._write("decisions", decision)
        if self.mode == "cloud":
            asyncio.create_task(self._cloud_post("/api/decisions", decision))
        return decision

    # ------------------------------------------------------------- work logs
    async def log_work(self, work: dict[str, Any]) -> dict[str, Any]:
        work.setdefault("id", str(uuid.uuid4()))
        work["agent_id"] = self.agent_id
        work["created_at"] = _now()
        self._write("work_logs", work)
        if self.mode == "cloud":
            asyncio.create_task(self._cloud_post("/api/work-logs", work))
        return work

    # ------------------------------------------------------------ code atlas
    async def trigger_scan(self, project_id: str) -> dict[str, Any]:
        scan = {
            "id": str(uuid.uuid4()),
            "project_id": project_id,
            "agent_id": self.agent_id,
            "status": "pending",
            "total_files": 0,
            "scanned_files": 0,
            "started_at": _now(),
        }
        self._write("code_atlas_scans", scan)
        if self.mode == "cloud":
            asyncio.create_task(self._cloud_post("/api/code-atlas/scan", scan))
        return scan

    async def update_scan(self, scan_id: str, updates: dict[str, Any]) -> None:
        rows = self._query("code_atlas_scans", "id=?", (scan_id,))
        if not rows:
            return
        self._write("code_atlas_scans", {**rows[0], **updates, "id": scan_id})
        if self.mode == "cloud":
            asyncio.create_task(self._cloud_put(f"/api/code-atlas/scan/{scan_id}", updates))

    async def add_scan_files(self, files: list[dict[str, Any]]) -> None:
        for f in files:
            f.setdefault("id", str(uuid.uuid4()))
            f["scanned_at"] = _now()
            self._write("code_atlas_files", f)
        if self.mode == "cloud":
            asyncio.create_task(
                self._cloud_post("/api/code-atlas/files/bulk", {"files": files})
            )

    # --------------------------------------------------------- knowledge graph
    async def add_nodes(self, nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
        for n in nodes:
            n.setdefault("id", str(uuid.uuid4()))
            n["agent_id"] = self.agent_id
            n["created_at"] = _now()
            self._write("knowledge_nodes", n)
        if self.mode == "cloud":
            asyncio.create_task(
                self._cloud_post("/api/knowledge-graph/nodes/bulk", {"nodes": nodes})
            )
        return nodes

    async def add_edges(self, edges: list[dict[str, Any]]) -> list[dict[str, Any]]:
        for e in edges:
            e.setdefault("id", str(uuid.uuid4()))
            e["created_at"] = _now()
            self._write("knowledge_edges", e)
        if self.mode == "cloud":
            asyncio.create_task(
                self._cloud_post("/api/knowledge-graph/edges/bulk", {"edges": edges})
            )
        return edges

    def query_graph(self) -> dict[str, list[dict[str, Any]]]:
        return {
            "nodes": self._query("knowledge_nodes"),
            "edges": self._query("knowledge_edges"),
        }

    # ------------------------------------------------------ performance logs
    async def ingest_performance_logs(self, logs: list[dict[str, Any]]) -> None:
        for log in logs:
            log.setdefault("id", str(uuid.uuid4()))
            log["agent_id"] = self.agent_id
            log["logged_at"] = _now()
            self._write("performance_logs", log)
        if self.mode == "cloud" and logs:
            asyncio.create_task(
                self._cloud_post("/api/performance-logs", {"logs": logs})
            )

    def get_performance_summary(self, limit: int = 50) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM performance_logs ORDER BY logged_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]
