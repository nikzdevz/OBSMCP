from __future__ import annotations

import json
import re
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import AppConfig, ProjectConfig
from .database import Database
from .utils import parse_json, read_text_with_retry, slugify, utc_now, write_json_atomic, write_text_atomic


DEFAULT_BRIEF_SECTIONS = {
    "Mission": "Describe the project mission and intended business or engineering outcome.",
    "Success Criteria": "Define the outcome that marks the current phase as successful.",
    "Architecture": "Capture the current architecture, constraints, and integration boundaries.",
    "Working Agreements": "Record conventions that every model, agent, or teammate should preserve.",
}

DEFAULT_TASK_TEMPLATES = [
    {
        "name": "bug",
        "title_template": "Bug: {summary}",
        "description_template": "## Summary\n{summary}\n\n## Steps to Reproduce\n{steps}\n\n## Expected Behavior\n{expected}\n\n## Actual Behavior\n{actual}",
        "priority": "high",
        "tags": ["bug"],
    },
    {
        "name": "feature",
        "title_template": "Feature: {name}",
        "description_template": "## Goal\n{goal}\n\n## Acceptance Criteria\n{criteria}\n\n## Notes\n{notes}",
        "priority": "medium",
        "tags": ["feature"],
    },
    {
        "name": "research",
        "title_template": "Research: {topic}",
        "description_template": "## Research Topic\n{topic}\n\n## Question\n{question}\n\n## Findings\n{finding}\n\n## Conclusion\n{conclusion}",
        "priority": "low",
        "tags": ["research"],
    },
    {
        "name": "refactor",
        "title_template": "Refactor: {target}",
        "description_template": "## Target\n{target}\n\n## Reason\n{reason}\n\n## Approach\n{approach}\n\n## Risk Assessment\n{risk}",
        "priority": "medium",
        "tags": ["refactor"],
    },
    {
        "name": "docs",
        "title_template": "Document: {target}",
        "description_template": "## Document Target\n{target}\n\n## Purpose\n{purpose}\n\n## Outline\n{outline}",
        "priority": "low",
        "tags": ["documentation"],
    },
    {
        "name": "test",
        "title_template": "Test: {target}",
        "description_template": "## What to Test\n{what_to_test}\n\n## Test Cases\n{test_cases}\n\n## Edge Cases\n{edge_cases}",
        "priority": "medium",
        "tags": ["testing"],
    },
]

CHECKPOINT_TOKEN_RE = re.compile(r"\b([A-Z]{1,4}\d+(?:-[A-Z0-9]+)+)\b")
CHECKPOINT_PHASE_RE = re.compile(r"^([A-Z]+[0-9]+)")


class StateStore:
    def __init__(self, config: AppConfig, project_config: ProjectConfig | None = None) -> None:
        self.config = config
        self.project_config = project_config or config.get_project_config(None)
        self.project_root = str(self.project_config.project_path.resolve())
        self._ensure_dirs()
        self.database = Database(self.project_config.db_path)
        self.database.initialize()
        self._migrate_schema()
        self._bootstrap()

    def _migrate_schema(self) -> None:
        """Auto-migrate schema for projects created with older versions.

        Detects missing columns in semantic_descriptions table and adds them.
        This ensures older projects get new columns (llm_model, etc.) without
        requiring manual schema updates.
        """
        SEMANTIC_COLUMNS = [
            ("llm_model", "TEXT"),
            ("llm_latency_ms", "REAL"),
            ("llm_input_tokens", "INTEGER"),
            ("llm_output_tokens", "INTEGER"),
            ("llm_generated", "INTEGER NOT NULL DEFAULT 0"),
            ("language", "TEXT"),
        ]
        SESSION_COLUMNS = [
            ("session_label", "TEXT NOT NULL DEFAULT ''"),
            ("workstream_key", "TEXT NOT NULL DEFAULT ''"),
            ("workstream_title", "TEXT NOT NULL DEFAULT ''"),
        ]
        try:
            with self._connect() as connection:
                cursor = connection.execute("PRAGMA table_info(semantic_descriptions)")
                existing = {row[1] for row in cursor.fetchall()}
                for col_name, col_type in SEMANTIC_COLUMNS:
                    if col_name not in existing:
                        connection.execute(f"ALTER TABLE semantic_descriptions ADD COLUMN {col_name} {col_type}")
                cursor = connection.execute("PRAGMA table_info(sessions)")
                existing = {row[1] for row in cursor.fetchall()}
                for col_name, col_type in SESSION_COLUMNS:
                    if col_name not in existing:
                        connection.execute(f"ALTER TABLE sessions ADD COLUMN {col_name} {col_type}")
                connection.execute(
                    """
                    UPDATE sessions
                    SET session_label = COALESCE(NULLIF(session_label, ''), id),
                        workstream_key = COALESCE(NULLIF(workstream_key, ''), lower(replace(replace(client_name, ' ', '-'), '_', '-'))),
                        workstream_title = COALESCE(NULLIF(workstream_title, ''), NULLIF(session_label, ''), id)
                    """
                )
                connection.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_sessions_workstream_status
                    ON sessions(workstream_key, status, heartbeat_at DESC)
                    """
                )
                connection.commit()
        except Exception:
            pass  # Silently skip if table doesn't exist yet

    def _ensure_dirs(self) -> None:
        """Create per-project directory structure on demand."""
        base_paths = {
            self.project_config.workspace_root,
            self.project_config.data_root,
            self.project_config.db_path.parent,
            self.project_config.json_export_dir,
            self.project_config.backup_dir,
            self.project_config.export_dir,
            self.project_config.log_dir,
            self.project_config.vault_path,
            self.project_config.context_path,
            self.project_config.sessions_path,
        }
        paths: set[Path] = set()
        for path in base_paths:
            current = path
            while True:
                paths.add(current)
                if current == self.project_config.workspace_root or current.parent == current:
                    break
                current = current.parent
        for path in sorted(paths, key=lambda item: (len(item.parts), str(item))):
            if path.exists():
                continue
            path.mkdir(exist_ok=True)

    def _session_dir(self, session_id: str) -> Path:
        return self.project_config.sessions_path / session_id

    def _write_project_manifest(self) -> None:
        payload = {
            "project_slug": self.project_config.project_slug,
            "project_name": self.project_config.project_name,
            "repo_path": str(self.project_config.project_path),
            "workspace_root": str(self.project_config.workspace_root),
            "db_path": str(self.project_config.db_path),
            "vault_path": str(self.project_config.vault_path),
            "context_path": str(self.project_config.context_path),
            "sessions_path": str(self.project_config.sessions_path),
            "updated_at": utc_now(),
        }
        write_json_atomic(self.project_config.manifest_path, payload)

    def _write_session_metadata(self, session_id: str, payload: dict[str, Any]) -> None:
        session_dir = self._session_dir(session_id)
        session_dir.mkdir(parents=True, exist_ok=True)
        write_json_atomic(session_dir / "metadata.json", payload)

    def _append_session_jsonl(self, session_id: str, filename: str, payload: dict[str, Any]) -> None:
        session_dir = self._session_dir(session_id)
        session_dir.mkdir(parents=True, exist_ok=True)
        path = session_dir / filename
        existing = read_text_with_retry(path, default="") if path.exists() else ""
        line = json.dumps(payload, ensure_ascii=True)
        write_text_atomic(path, existing + line + "\n")

    def _append_session_markdown(self, session_id: str, filename: str, entry: str, heading: str) -> None:
        session_dir = self._session_dir(session_id)
        session_dir.mkdir(parents=True, exist_ok=True)
        path = session_dir / filename
        existing = read_text_with_retry(path, default=f"# {heading}\n\n") if path.exists() else f"# {heading}\n\n"
        if not existing.endswith("\n"):
            existing += "\n"
        write_text_atomic(path, existing + entry.rstrip() + "\n\n")

    def _connect(self) -> sqlite3.Connection:
        return self.database.connect()

    def _parse_utc(self, value: str | None) -> datetime | None:
        if not value:
            return None
        return datetime.fromisoformat(value.replace("Z", "+00:00"))

    def _normalize_project_file_path(self, file_path: str) -> str:
        candidate = Path(file_path)
        if candidate.is_absolute():
            try:
                return str(candidate.resolve().relative_to(self.project_config.project_path.resolve())).replace("\\", "/")
            except ValueError:
                return str(candidate.resolve()).replace("\\", "/")
        return file_path.replace("\\", "/")

    def _bootstrap(self) -> None:
        now = utc_now()
        with self._connect() as connection:
            for section, content in DEFAULT_BRIEF_SECTIONS.items():
                connection.execute(
                    """
                    INSERT OR IGNORE INTO project_brief_sections (section, content, updated_at)
                    VALUES (?, ?, ?)
                    """,
                    (section, content, now),
                )
            connection.execute(
                """
                INSERT OR IGNORE INTO project_state (key, value, updated_at)
                VALUES ('current_task_id', '', ?)
                """,
                (now,),
            )
            for tmpl in DEFAULT_TASK_TEMPLATES:
                connection.execute(
                    """
                    INSERT OR IGNORE INTO task_templates
                    (name, title_template, description_template, priority, tags, created_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        tmpl["name"],
                        tmpl["title_template"],
                        tmpl["description_template"],
                        tmpl["priority"],
                        json.dumps(tmpl["tags"]),
                        now,
                    ),
                )
            connection.execute(
                """
                UPDATE scan_jobs
                SET status = 'interrupted',
                    finished_at = COALESCE(finished_at, ?),
                    error_text = CASE
                        WHEN error_text = '' THEN 'Server restarted before the scan job finished.'
                        ELSE error_text
                    END,
                    progress_message = CASE
                        WHEN progress_message = '' THEN 'Interrupted by server restart.'
                        ELSE progress_message
                    END
                WHERE status IN ('queued', 'running')
                """,
                (now,),
            )
            connection.commit()
        self._write_project_manifest()

    def _fetchone_dict(self, connection: sqlite3.Connection, query: str, params: tuple[Any, ...] = ()) -> dict[str, Any] | None:
        row = connection.execute(query, params).fetchone()
        return dict(row) if row else None

    def _fetchall_dicts(self, connection: sqlite3.Connection, query: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        rows = connection.execute(query, params).fetchall()
        return [dict(row) for row in rows]

    def _normalize_task(self, task: dict[str, Any] | None) -> dict[str, Any] | None:
        if not task:
            return None
        task["relevant_files"] = parse_json(task.get("relevant_files"), [])
        task["tags"] = parse_json(task.get("tags"), [])
        return task

    def _normalize_session(self, session: dict[str, Any] | None) -> dict[str, Any] | None:
        if not session:
            return None
        session["session_label"] = (session.get("session_label") or session.get("id") or "").strip()
        session["workstream_key"] = (session.get("workstream_key") or "").strip()
        session["workstream_title"] = (session.get("workstream_title") or session.get("session_label") or session.get("id") or "").strip()
        return session

    def _normalize_work_log(self, row: dict[str, Any]) -> dict[str, Any]:
        row["files"] = parse_json(row.get("files"), [])
        return row

    def _normalize_checkpoint(self, row: dict[str, Any] | None) -> dict[str, Any] | None:
        if not row:
            return None
        row["files"] = parse_json(row.get("files"), [])
        return row

    def _extract_expected_checkpoint_ids(self, task: dict[str, Any] | None) -> list[str]:
        if not task:
            return []
        text = "\n".join([str(task.get("title", "")), str(task.get("description", ""))])
        expected: list[str] = []
        seen: set[str] = set()
        for match in CHECKPOINT_TOKEN_RE.finditer(text):
            checkpoint_id = match.group(1)
            if checkpoint_id in seen:
                continue
            seen.add(checkpoint_id)
            expected.append(checkpoint_id)
        return expected

    def _checkpoint_phase_key(self, checkpoint_id: str) -> str:
        if "-" in checkpoint_id:
            return checkpoint_id.split("-", 1)[0]
        match = CHECKPOINT_PHASE_RE.match(checkpoint_id)
        if match:
            return match.group(1)
        return checkpoint_id

    def _normalize_semantic_index_row(self, row: dict[str, Any] | None) -> dict[str, Any] | None:
        if not row:
            return None
        row["feature_tags"] = parse_json(row.get("feature_tags"), [])
        row["source_files"] = parse_json(row.get("source_files"), [])
        row["metadata"] = parse_json(row.get("metadata"), {})
        return row

    def _normalize_semantic_description(self, row: dict[str, Any] | None) -> dict[str, Any] | None:
        if not row:
            return None
        row["file"] = row.get("file_path", "")
        row["related_files"] = parse_json(row.get("related_files"), [])
        row["related_decisions"] = parse_json(row.get("related_decisions"), [])
        row["related_tasks"] = parse_json(row.get("related_tasks"), [])
        row["related_symbols"] = parse_json(row.get("related_symbols"), [])
        row["metadata"] = parse_json(row.get("metadata"), {})
        row["freshness"] = "stale" if row.get("stale") else "fresh"
        row["llm_generated"] = bool(row.get("llm_generated"))
        return row

    def _normalize_scan_job(self, row: dict[str, Any] | None) -> dict[str, Any] | None:
        if not row:
            return None
        row["force_refresh"] = bool(row.get("force_refresh"))
        row["result"] = parse_json(row.get("result_json"), {})
        return row

    def _normalize_context_artifact(self, row: dict[str, Any] | None) -> dict[str, Any] | None:
        if not row:
            return None
        row["metadata"] = parse_json(row.get("metadata"), {})
        return row

    def _normalize_token_usage_event(self, row: dict[str, Any] | None) -> dict[str, Any] | None:
        if not row:
            return None
        row["metadata"] = parse_json(row.get("metadata"), {})
        return row

    def _normalize_raw_output_capture(self, row: dict[str, Any] | None) -> dict[str, Any] | None:
        if not row:
            return None
        row["metadata"] = parse_json(row.get("metadata"), {})
        return row

    def _normalize_command_event(self, row: dict[str, Any] | None) -> dict[str, Any] | None:
        if not row:
            return None
        row["files_changed"] = parse_json(row.get("files_changed"), [])
        row["metadata"] = parse_json(row.get("metadata"), {})
        row["raw_output_available"] = bool(row.get("raw_output_available"))
        return row

    def _record_activity(self, connection: sqlite3.Connection, actor: str, action: str, task_id: str | None, payload: dict[str, Any]) -> None:
        connection.execute(
            """
            INSERT INTO agent_activity (actor, action, task_id, payload, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (actor, action, task_id, json.dumps(payload), utc_now()),
        )

    def _record_session_event(
        self,
        connection: sqlite3.Connection,
        session_id: str,
        actor: str,
        event_type: str,
        payload: dict[str, Any],
    ) -> None:
        connection.execute(
            """
            INSERT INTO session_events (session_id, actor, event_type, payload, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (session_id, actor, event_type, json.dumps(payload), utc_now()),
        )

    def _touch_session_write(
        self,
        connection: sqlite3.Connection,
        session_id: str | None,
        actor: str,
        event_type: str,
        payload: dict[str, Any],
    ) -> None:
        if not session_id:
            return
        now = utc_now()
        connection.execute(
            """
            UPDATE sessions
            SET write_count = write_count + 1,
                last_write_at = ?,
                heartbeat_at = ?
            WHERE id = ?
            """,
            (now, now, session_id),
        )
        self._record_session_event(connection, session_id, actor, event_type, payload)

    def get_project_brief(self) -> dict[str, str]:
        with self._connect() as connection:
            rows = self._fetchall_dicts(
                connection,
                "SELECT section, content FROM project_brief_sections ORDER BY section ASC",
            )
        return {row["section"]: row["content"] for row in rows}

    def update_project_brief_section(
        self,
        section: str,
        content: str,
        actor: str = "unknown",
        session_id: str | None = None,
    ) -> dict[str, str]:
        now = utc_now()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO project_brief_sections (section, content, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(section) DO UPDATE SET content = excluded.content, updated_at = excluded.updated_at
                """,
                (section, content, now),
            )
            self._record_activity(connection, actor, "update_project_brief_section", None, {"section": section})
            self._touch_session_write(
                connection,
                session_id=session_id,
                actor=actor,
                event_type="update_project_brief_section",
                payload={"section": section},
            )
            connection.commit()
        return self.get_project_brief()

    def create_task(
        self,
        title: str,
        description: str,
        priority: str = "medium",
        owner: str | None = None,
        relevant_files: list[str] | None = None,
        tags: list[str] | None = None,
        actor: str = "unknown",
        session_id: str | None = None,
    ) -> dict[str, Any]:
        task_id = f"TASK-{uuid.uuid4().hex[:8].upper()}-{slugify(title, max_length=18)}"
        now = utc_now()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO tasks (
                    id, title, description, status, priority, owner, relevant_files, tags, created_at, updated_at
                ) VALUES (?, ?, ?, 'open', ?, ?, ?, ?, ?, ?)
                """,
                (
                    task_id,
                    title,
                    description,
                    priority,
                    owner,
                    json.dumps(relevant_files or []),
                    json.dumps(tags or []),
                    now,
                    now,
                ),
            )
            self._record_activity(connection, actor, "create_task", task_id, {"title": title})
            self._touch_session_write(
                connection,
                session_id=session_id,
                actor=actor,
                event_type="create_task",
                payload={"task_id": task_id, "title": title},
            )
            connection.commit()
        return self.get_task(task_id)

    def update_task(self, task_id: str, actor: str = "unknown", session_id: str | None = None, **fields: Any) -> dict[str, Any]:
        allowed = {"title", "description", "status", "priority", "owner", "relevant_files", "tags"}
        updates: list[str] = []
        values: list[Any] = []
        for key, value in fields.items():
            if key not in allowed or value is None:
                continue
            if key in {"relevant_files", "tags"}:
                value = json.dumps(value)
            updates.append(f"{key} = ?")
            values.append(value)

        if not updates:
            return self.get_task(task_id)

        now = utc_now()
        updates.append("updated_at = ?")
        values.append(now)

        status = fields.get("status")
        if status == "in_progress":
            updates.append("started_at = COALESCE(started_at, ?)")
            values.append(now)
        elif status == "done":
            updates.append("completed_at = ?")
            values.append(now)

        values.append(task_id)
        with self._connect() as connection:
            connection.execute(f"UPDATE tasks SET {', '.join(updates)} WHERE id = ?", tuple(values))
            self._record_activity(connection, actor, "update_task", task_id, fields)
            self._touch_session_write(
                connection,
                session_id=session_id,
                actor=actor,
                event_type="update_task",
                payload={"task_id": task_id, "fields": list(fields.keys())},
            )
            connection.commit()
        return self.get_task(task_id)

    def get_task(self, task_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            task = self._fetchone_dict(connection, "SELECT * FROM tasks WHERE id = ?", (task_id,))
        return self._normalize_task(task)

    def set_current_task(self, task_id: str, actor: str = "unknown", session_id: str | None = None) -> dict[str, Any] | None:
        now = utc_now()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO project_state (key, value, updated_at)
                VALUES ('current_task_id', ?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
                """,
                (task_id, now),
            )
            connection.execute(
                """
                UPDATE tasks
                SET status = CASE WHEN status = 'open' THEN 'in_progress' ELSE status END,
                    updated_at = ?,
                    started_at = COALESCE(started_at, ?)
                WHERE id = ?
                """,
                (now, now, task_id),
            )
            self._record_activity(connection, actor, "set_current_task", task_id, {})
            self._touch_session_write(
                connection,
                session_id=session_id,
                actor=actor,
                event_type="set_current_task",
                payload={"task_id": task_id},
            )
            connection.commit()
        return self.get_task(task_id)

    def get_current_task(self) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = self._fetchone_dict(connection, "SELECT value FROM project_state WHERE key = 'current_task_id'")
            if not row or not row["value"]:
                return None
            task = self._fetchone_dict(connection, "SELECT * FROM tasks WHERE id = ?", (row["value"],))
        return self._normalize_task(task)

    def get_active_tasks(self, limit: int = 20) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = self._fetchall_dicts(
                connection,
                """
                SELECT * FROM tasks
                WHERE status IN ('open', 'in_progress', 'blocked')
                ORDER BY
                    CASE status
                        WHEN 'in_progress' THEN 0
                        WHEN 'blocked' THEN 1
                        ELSE 2
                    END,
                    updated_at DESC
                LIMIT ?
                """,
                (limit,),
            )
        return [self._normalize_task(row) for row in rows]

    def log_work(
        self,
        message: str,
        actor: str = "unknown",
        task_id: str | None = None,
        summary: str | None = None,
        files: list[str] | None = None,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        now = utc_now()
        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO work_logs (task_id, actor, message, summary, files, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (task_id, actor, message, summary, json.dumps(files or []), now),
            )
            work_log_id = cursor.lastrowid
            self._record_activity(connection, actor, "log_work", task_id, {"summary": summary, "files": files or []})
            self._touch_session_write(
                connection,
                session_id=session_id,
                actor=actor,
                event_type="log_work",
                payload={"task_id": task_id, "message": message, "files": files or []},
            )
            connection.commit()
            row = self._fetchone_dict(connection, "SELECT * FROM work_logs WHERE id = ?", (work_log_id,))
        payload = self._normalize_work_log(row or {})
        if session_id:
            self._append_session_markdown(
                session_id,
                "worklog.md",
                f"- {payload.get('created_at', utc_now())} [{actor}] {message}",
                "Session Worklog",
            )
        return payload

    def get_recent_work(self, limit: int = 10, after_id: int | None = None) -> list[dict[str, Any]]:
        params: list[Any] = []
        if after_id is not None:
            params.append(after_id)
        with self._connect() as connection:
            if after_id is not None:
                rows = self._fetchall_dicts(
                    connection,
                    """
                    SELECT * FROM work_logs
                    WHERE id < ?
                    ORDER BY created_at DESC
                    LIMIT ?
                    """,
                    (after_id, limit),
                )
            else:
                rows = self._fetchall_dicts(
                    connection,
                    """
                    SELECT * FROM work_logs
                    ORDER BY created_at DESC
                    LIMIT ?
                    """,
                    (limit,),
                )
        return [self._normalize_work_log(row) for row in rows]

    def log_checkpoint(
        self,
        task_id: str,
        checkpoint_id: str,
        title: str,
        message: str = "",
        status: str = "completed",
        files: list[str] | None = None,
        actor: str = "unknown",
        session_id: str | None = None,
    ) -> dict[str, Any]:
        now = utc_now()
        normalized_files = [self._normalize_project_file_path(item) for item in (files or [])]
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO checkpoints (task_id, checkpoint_id, title, status, message, files, actor, session_id, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(task_id, checkpoint_id) DO UPDATE SET
                    title = excluded.title,
                    status = excluded.status,
                    message = excluded.message,
                    files = excluded.files,
                    actor = excluded.actor,
                    session_id = excluded.session_id,
                    created_at = excluded.created_at
                """,
                (task_id, checkpoint_id, title, status, message, json.dumps(normalized_files), actor, session_id, now),
            )
            self._record_activity(
                connection,
                actor,
                "log_checkpoint",
                task_id,
                {"checkpoint_id": checkpoint_id, "title": title, "status": status, "files": normalized_files},
            )
            self._touch_session_write(
                connection,
                session_id=session_id,
                actor=actor,
                event_type="log_checkpoint",
                payload={"task_id": task_id, "checkpoint_id": checkpoint_id, "title": title, "status": status},
            )
            connection.commit()
            row = self._fetchone_dict(
                connection,
                "SELECT * FROM checkpoints WHERE task_id = ? AND checkpoint_id = ?",
                (task_id, checkpoint_id),
            )
        payload = self._normalize_checkpoint(row)
        if session_id and payload:
            self._append_session_markdown(
                session_id,
                "worklog.md",
                f"- {payload.get('created_at', now)} [{actor}] Checkpoint {checkpoint_id}: {title}",
                "Session Worklog",
            )
        return payload or {}

    def get_checkpoints_for_task(self, task_id: str, limit: int = 50) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = self._fetchall_dicts(
                connection,
                """
                SELECT * FROM checkpoints
                WHERE task_id = ?
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (task_id, limit),
            )
        return [self._normalize_checkpoint(row) for row in rows if row]

    def get_recent_checkpoints(self, limit: int = 20, task_id: str | None = None) -> list[dict[str, Any]]:
        with self._connect() as connection:
            if task_id:
                rows = self._fetchall_dicts(
                    connection,
                    """
                    SELECT * FROM checkpoints
                    WHERE task_id = ?
                    ORDER BY created_at DESC
                    LIMIT ?
                    """,
                    (task_id, limit),
                )
            else:
                rows = self._fetchall_dicts(
                    connection,
                    """
                    SELECT * FROM checkpoints
                    ORDER BY created_at DESC
                    LIMIT ?
                    """,
                    (limit,),
                )
        return [self._normalize_checkpoint(row) for row in rows if row]

    def get_checkpoint_progress(self, task_id: str) -> dict[str, Any]:
        task = self.get_task(task_id)
        expected_ids = self._extract_expected_checkpoint_ids(task)
        checkpoints = self.get_checkpoints_for_task(task_id, limit=500)
        completed_items = [item for item in checkpoints if item.get("status") == "completed"]
        completed_ids = {item["checkpoint_id"] for item in completed_items}
        latest_completed_at = completed_items[0]["created_at"] if completed_items else None

        if expected_ids:
            expected_set = set(expected_ids)
            completed_expected = [checkpoint_id for checkpoint_id in expected_ids if checkpoint_id in completed_ids]
            remaining_ids = [checkpoint_id for checkpoint_id in expected_ids if checkpoint_id not in completed_ids]
            completed_count = len(completed_expected)
            total_count: int | None = len(expected_ids)
        else:
            completed_count = len(completed_ids)
            total_count = None
            remaining_ids = []

        phase_rollups: dict[str, dict[str, Any]] = {}
        phase_order: list[str] = []
        for checkpoint_id in expected_ids or sorted(completed_ids):
            phase_key = self._checkpoint_phase_key(checkpoint_id)
            if phase_key not in phase_rollups:
                phase_rollups[phase_key] = {
                    "phase_key": phase_key,
                    "completed_count": 0,
                    "total_count": 0 if expected_ids else None,
                    "complete": False,
                    "remaining_checkpoints": [],
                }
                phase_order.append(phase_key)
            rollup = phase_rollups[phase_key]
            if expected_ids:
                rollup["total_count"] = int(rollup["total_count"] or 0) + 1
                if checkpoint_id in completed_ids:
                    rollup["completed_count"] += 1
                else:
                    rollup["remaining_checkpoints"].append(checkpoint_id)

        if not expected_ids:
            for checkpoint_id in sorted(completed_ids):
                phase_key = self._checkpoint_phase_key(checkpoint_id)
                if phase_key not in phase_rollups:
                    phase_rollups[phase_key] = {
                        "phase_key": phase_key,
                        "completed_count": 0,
                        "total_count": None,
                        "complete": False,
                        "remaining_checkpoints": [],
                    }
                    phase_order.append(phase_key)
                phase_rollups[phase_key]["completed_count"] += 1

        grouped_progress: list[dict[str, Any]] = []
        for phase_key in phase_order:
            rollup = phase_rollups[phase_key]
            total = rollup["total_count"]
            complete = bool(total is not None and total > 0 and rollup["completed_count"] >= total)
            grouped_progress.append(
                {
                    **rollup,
                    "complete": complete,
                    "completion_ratio": (rollup["completed_count"] / total) if total else None,
                }
            )

        all_expected_complete = bool(total_count is not None and total_count > 0 and completed_count >= total_count)
        return {
            "task_id": task_id,
            "completed_count": completed_count,
            "latest_completed_at": latest_completed_at,
            "expected_checkpoints": expected_ids,
            "remaining_checkpoints": remaining_ids,
            "total_count": total_count,
            "all_expected_complete": all_expected_complete,
            "completion_ratio": (completed_count / total_count) if total_count else None,
            "phase_rollups": grouped_progress,
        }

    def log_decision(
        self,
        title: str,
        decision: str,
        rationale: str = "",
        impact: str = "",
        task_id: str | None = None,
        actor: str = "unknown",
        session_id: str | None = None,
    ) -> dict[str, Any]:
        now = utc_now()
        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO decisions (title, decision, rationale, impact, task_id, actor, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (title, decision, rationale, impact, task_id, actor, now),
            )
            decision_id = cursor.lastrowid
            self._record_activity(connection, actor, "log_decision", task_id, {"title": title})
            self._touch_session_write(
                connection,
                session_id=session_id,
                actor=actor,
                event_type="log_decision",
                payload={"task_id": task_id, "title": title},
            )
            connection.commit()
            row = self._fetchone_dict(connection, "SELECT * FROM decisions WHERE id = ?", (decision_id,))
        return row or {}

    def get_decisions(self, limit: int = 10, after_id: int | None = None) -> list[dict[str, Any]]:
        with self._connect() as connection:
            if after_id is not None:
                return self._fetchall_dicts(
                    connection,
                    "SELECT * FROM decisions WHERE id < ? ORDER BY created_at DESC LIMIT ?",
                    (after_id, limit),
                )
            return self._fetchall_dicts(
                connection,
                "SELECT * FROM decisions ORDER BY created_at DESC LIMIT ?",
                (limit,),
            )

    def log_blocker(
        self,
        title: str,
        description: str,
        task_id: str | None = None,
        actor: str = "unknown",
        session_id: str | None = None,
    ) -> dict[str, Any]:
        now = utc_now()
        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO blockers (title, description, task_id, actor, status, created_at)
                VALUES (?, ?, ?, ?, 'open', ?)
                """,
                (title, description, task_id, actor, now),
            )
            blocker_id = cursor.lastrowid
            if task_id:
                connection.execute("UPDATE tasks SET status = 'blocked', updated_at = ? WHERE id = ?", (now, task_id))
            self._record_activity(connection, actor, "log_blocker", task_id, {"title": title})
            self._touch_session_write(
                connection,
                session_id=session_id,
                actor=actor,
                event_type="log_blocker",
                payload={"task_id": task_id, "title": title},
            )
            connection.commit()
            row = self._fetchone_dict(connection, "SELECT * FROM blockers WHERE id = ?", (blocker_id,))
        return row or {}

    def resolve_blocker(self, blocker_id: int, resolution_note: str, actor: str = "unknown") -> dict[str, Any] | None:
        now = utc_now()
        with self._connect() as connection:
            blocker = self._fetchone_dict(connection, "SELECT * FROM blockers WHERE id = ?", (blocker_id,))
            if not blocker:
                return None
            connection.execute(
                """
                UPDATE blockers
                SET status = 'resolved', resolution_note = ?, resolved_at = ?
                WHERE id = ?
                """,
                (resolution_note, now, blocker_id),
            )
            if blocker.get("task_id"):
                connection.execute(
                    """
                    UPDATE tasks
                    SET status = CASE WHEN status = 'blocked' THEN 'in_progress' ELSE status END,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (now, blocker["task_id"]),
                )
            self._record_activity(connection, actor, "resolve_blocker", blocker.get("task_id"), {"blocker_id": blocker_id})
            connection.commit()
            return self._fetchone_dict(connection, "SELECT * FROM blockers WHERE id = ?", (blocker_id,))

    def get_blockers(self, open_only: bool = True, limit: int = 20, after_id: int | None = None) -> list[dict[str, Any]]:
        query = "SELECT * FROM blockers"
        params: list[Any] = []
        conditions: list[str] = []
        if open_only:
            conditions.append("status = 'open'")
        if after_id is not None:
            conditions.append("id < ?")
            params.append(after_id)
        if conditions:
            query += " WHERE " + " AND ".join(conditions)
        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        with self._connect() as connection:
            return self._fetchall_dicts(connection, query, params)

    def create_handoff(
        self,
        summary: str,
        next_steps: str = "",
        open_questions: str = "",
        note: str = "",
        task_id: str | None = None,
        from_actor: str = "unknown",
        to_actor: str = "next-agent",
        session_id: str | None = None,
    ) -> dict[str, Any]:
        now = utc_now()
        with self._connect() as connection:
            session = self._fetchone_dict(connection, "SELECT * FROM sessions WHERE id = ?", (session_id,)) if session_id else None
            cursor = connection.execute(
                """
                INSERT INTO handoffs (
                    task_id, from_actor, to_actor, summary, next_steps, open_questions, note, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (task_id, from_actor, to_actor, summary, next_steps, open_questions, note, now, now),
            )
            handoff_id = cursor.lastrowid
            connection.execute(
                """
                INSERT INTO session_summaries (session_label, summary, actor, created_at)
                VALUES (?, ?, ?, ?)
                """,
                ((session or {}).get("session_label") or "handoff", summary, from_actor, now),
            )
            self._record_activity(connection, from_actor, "create_handoff", task_id, {"to_actor": to_actor})
            if session_id:
                connection.execute(
                    """
                    UPDATE sessions
                    SET write_count = write_count + 1,
                        last_write_at = ?,
                        heartbeat_at = ?,
                        last_handoff_at = ?,
                        handoff_created = 1
                    WHERE id = ?
                    """,
                    (now, now, now, session_id),
                )
                self._record_session_event(
                    connection,
                    session_id=session_id,
                    actor=from_actor,
                    event_type="create_handoff",
                    payload={"task_id": task_id, "to_actor": to_actor},
                )
            connection.commit()
            handoff = self._fetchone_dict(connection, "SELECT * FROM handoffs WHERE id = ?", (handoff_id,)) or {}
        if session_id and handoff:
            handoff_lines = [
                "# Handoff",
                "",
                f"- ID: {handoff['id']}",
                f"- From: {handoff['from_actor']}",
                f"- To: {handoff['to_actor']}",
                f"- Task: {handoff['task_id'] or 'unassigned'}",
                f"- Created: {handoff['created_at']}",
                "",
                "## Summary",
                "",
                handoff["summary"],
                "",
                "## Next Steps",
                "",
                handoff["next_steps"] or "None recorded.",
                "",
                "## Open Questions",
                "",
                handoff["open_questions"] or "None recorded.",
                "",
                "## Notes",
                "",
                handoff["note"] or "None recorded.",
                "",
            ]
            write_text_atomic(self._session_dir(session_id) / "handoff.md", "\n".join(handoff_lines))
        return handoff

    def append_handoff_note(self, handoff_id: int, note: str, actor: str = "unknown") -> dict[str, Any] | None:
        now = utc_now()
        with self._connect() as connection:
            current = self._fetchone_dict(connection, "SELECT * FROM handoffs WHERE id = ?", (handoff_id,))
            if not current:
                return None
            updated_note = f"{current['note'].rstrip()}\n\n{note}".strip()
            connection.execute(
                "UPDATE handoffs SET note = ?, updated_at = ? WHERE id = ?",
                (updated_note, now, handoff_id),
            )
            self._record_activity(connection, actor, "append_handoff_note", current.get("task_id"), {"handoff_id": handoff_id})
            connection.commit()
            return self._fetchone_dict(connection, "SELECT * FROM handoffs WHERE id = ?", (handoff_id,))

    def get_latest_handoff(self) -> dict[str, Any] | None:
        with self._connect() as connection:
            return self._fetchone_dict(connection, "SELECT * FROM handoffs ORDER BY created_at DESC LIMIT 1")

    def get_handoff(self, handoff_id: int) -> dict[str, Any] | None:
        with self._connect() as connection:
            return self._fetchone_dict(connection, "SELECT * FROM handoffs WHERE id = ?", (handoff_id,))

    def get_recent_handoffs(self, limit: int = 5) -> list[dict[str, Any]]:
        with self._connect() as connection:
            return self._fetchall_dicts(
                connection,
                "SELECT * FROM handoffs ORDER BY created_at DESC LIMIT ?",
                (limit,),
            )

    def create_daily_note_entry(
        self,
        entry: str,
        actor: str = "unknown",
        note_date: str | None = None,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        now = utc_now()
        note_date = note_date or now[:10]
        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO daily_entries (note_date, actor, entry, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (note_date, actor, entry, now),
            )
            entry_id = cursor.lastrowid
            self._record_activity(connection, actor, "create_daily_note_entry", None, {"note_date": note_date})
            self._touch_session_write(
                connection,
                session_id=session_id,
                actor=actor,
                event_type="create_daily_note_entry",
                payload={"note_date": note_date, "entry": entry},
            )
            connection.commit()
            return self._fetchone_dict(connection, "SELECT * FROM daily_entries WHERE id = ?", (entry_id,)) or {}

    def get_daily_entries(self, note_date: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        query = "SELECT * FROM daily_entries"
        params: tuple[Any, ...] = ()
        if note_date:
            query += " WHERE note_date = ?"
            params = (note_date,)
        query += " ORDER BY created_at DESC LIMIT ?"
        params = params + (limit,)
        with self._connect() as connection:
            return self._fetchall_dicts(connection, query, params)

    def create_session_summary(self, summary: str, actor: str = "unknown", session_label: str = "manual") -> dict[str, Any]:
        now = utc_now()
        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO session_summaries (session_label, summary, actor, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (session_label, summary, actor, now),
            )
            summary_id = cursor.lastrowid
            self._record_activity(connection, actor, "create_session_summary", None, {"session_label": session_label})
            connection.commit()
            return self._fetchone_dict(connection, "SELECT * FROM session_summaries WHERE id = ?", (summary_id,)) or {}

    def get_latest_session_summary(self) -> dict[str, Any] | None:
        with self._connect() as connection:
            return self._fetchone_dict(connection, "SELECT * FROM session_summaries ORDER BY created_at DESC LIMIT 1")

    def open_session(
        self,
        actor: str,
        client_name: str = "",
        model_name: str = "",
        session_label: str = "",
        workstream_key: str = "",
        workstream_title: str = "",
        project_path: str = "",
        initial_request: str = "",
        session_goal: str = "",
        task_id: str | None = None,
        require_heartbeat: bool = True,
        require_work_log: bool = True,
        heartbeat_interval_seconds: int = 900,
        work_log_interval_seconds: int = 1800,
        min_work_logs: int = 1,
        handoff_required: bool = True,
        ide_name: str = "",
        ide_version: str = "",
        ide_platform: str = "",
        os_name: str = "",
        os_version: str = "",
    ) -> dict[str, Any]:
        now = utc_now()
        session_id = f"SESSION-{uuid.uuid4().hex[:12].upper()}"
        effective_project_path = project_path or self.project_root
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO sessions (
                    id, actor, client_name, model_name, session_label, workstream_key, workstream_title,
                    project_path, initial_request, session_goal, task_id,
                    status, opened_at, heartbeat_at, require_heartbeat, require_work_log,
                    heartbeat_interval_seconds, work_log_interval_seconds, min_work_logs, write_count,
                    handoff_required, handoff_created, closure_summary, last_error,
                    ide_name, ide_version, ide_platform, os_name, os_version
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, ?, ?, ?, ?, ?, ?, 0, ?, 0, '', '', ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    actor,
                    client_name,
                    model_name,
                    session_label,
                    workstream_key,
                    workstream_title,
                    effective_project_path,
                    initial_request,
                    session_goal,
                    task_id,
                    now,
                    now,
                    1 if require_heartbeat else 0,
                    1 if require_work_log else 0,
                    heartbeat_interval_seconds,
                    work_log_interval_seconds,
                    min_work_logs,
                    1 if handoff_required else 0,
                    ide_name,
                    ide_version,
                    ide_platform,
                    os_name,
                    os_version,
                ),
            )
            self._record_session_event(
                connection,
                session_id=session_id,
                actor=actor,
                event_type="session_open",
                payload={
                    "task_id": task_id,
                    "client_name": client_name,
                    "model_name": model_name,
                    "session_label": session_label,
                    "workstream_key": workstream_key,
                    "workstream_title": workstream_title,
                    "ide_name": ide_name,
                },
            )
            self._record_activity(connection, actor, "session_open", task_id, {"session_id": session_id})
            connection.commit()
        session = self.get_session(session_id) or {}
        self._write_session_metadata(session_id, session)
        self._append_session_jsonl(
            session_id,
            "heartbeat.jsonl",
            {
                "time": now,
                "event": "session_open",
                "actor": actor,
                "task_id": task_id,
                "project_path": effective_project_path,
                "session_label": session_label,
                "workstream_key": workstream_key,
                "ide_name": ide_name,
            },
        )
        return session

    def find_resumable_session(
        self,
        actor: str,
        project_path: str,
        client_name: str = "",
        model_name: str = "",
        workstream_key: str = "",
        max_age_seconds: int = 86400,
    ) -> dict[str, Any] | None:
        query = [
            "SELECT * FROM sessions WHERE status = 'open' AND actor = ? AND project_path = ?",
        ]
        params: list[Any] = [actor, project_path]
        if client_name:
            query.append("AND client_name = ?")
            params.append(client_name)
        if model_name:
            query.append("AND model_name = ?")
            params.append(model_name)
        if workstream_key:
            query.append("AND workstream_key = ?")
            params.append(workstream_key)
        query.append("ORDER BY heartbeat_at DESC LIMIT 10")
        with self._connect() as connection:
            rows = self._fetchall_dicts(connection, " ".join(query), tuple(params))
        now = datetime.now(timezone.utc)
        for row in rows:
            heartbeat_at = self._parse_utc(row.get("heartbeat_at"))
            if heartbeat_at is None:
                continue
            if int((now - heartbeat_at).total_seconds()) <= max_age_seconds:
                return row
        return None

    def resume_session(
        self,
        session_id: str,
        actor: str,
        *,
        client_name: str = "",
        model_name: str = "",
        session_label: str = "",
        workstream_key: str = "",
        workstream_title: str = "",
        task_id: str | None = None,
        initial_request: str = "",
        session_goal: str = "",
    ) -> dict[str, Any] | None:
        now = utc_now()
        with self._connect() as connection:
            session = self._fetchone_dict(connection, "SELECT * FROM sessions WHERE id = ?", (session_id,))
            if not session:
                return None
            connection.execute(
                """
                UPDATE sessions
                SET heartbeat_at = ?,
                    task_id = COALESCE(?, task_id),
                    client_name = CASE WHEN ? <> '' THEN ? ELSE client_name END,
                    model_name = CASE WHEN ? <> '' THEN ? ELSE model_name END,
                    session_label = CASE WHEN ? <> '' THEN ? ELSE session_label END,
                    workstream_key = CASE WHEN ? <> '' THEN ? ELSE workstream_key END,
                    workstream_title = CASE WHEN ? <> '' THEN ? ELSE workstream_title END,
                    initial_request = CASE WHEN ? <> '' THEN ? ELSE initial_request END,
                    session_goal = CASE WHEN ? <> '' THEN ? ELSE session_goal END,
                    last_error = ''
                WHERE id = ?
                """,
                (
                    now,
                    task_id,
                    client_name,
                    client_name,
                    model_name,
                    model_name,
                    session_label,
                    session_label,
                    workstream_key,
                    workstream_key,
                    workstream_title,
                    workstream_title,
                    initial_request,
                    initial_request,
                    session_goal,
                    session_goal,
                    session_id,
                ),
            )
            self._record_session_event(
                connection,
                session_id=session_id,
                actor=actor,
                event_type="session_resume",
                payload={
                    "task_id": task_id,
                    "client_name": client_name,
                    "model_name": model_name,
                    "session_label": session_label,
                    "workstream_key": workstream_key,
                    "workstream_title": workstream_title,
                },
            )
            self._record_activity(connection, actor, "session_resume", task_id or session.get("task_id"), {"session_id": session_id})
            connection.commit()
        payload = self.get_session(session_id)
        self._write_session_metadata(session_id, payload or {})
        self._append_session_jsonl(
            session_id,
            "heartbeat.jsonl",
            {
                "time": now,
                "event": "session_resume",
                "actor": actor,
                "task_id": task_id or (payload or {}).get("task_id"),
                "session_label": (payload or {}).get("session_label", ""),
                "workstream_key": (payload or {}).get("workstream_key", ""),
            },
        )
        return payload

    def heartbeat_session(
        self,
        session_id: str,
        actor: str,
        status_note: str = "",
        task_id: str | None = None,
        files: list[str] | None = None,
        create_work_log: bool = False,
    ) -> dict[str, Any] | None:
        now = utc_now()
        with self._connect() as connection:
            session = self._fetchone_dict(connection, "SELECT * FROM sessions WHERE id = ?", (session_id,))
            if not session:
                return None
            effective_task_id = task_id or session.get("task_id")
            connection.execute(
                """
                UPDATE sessions
                SET heartbeat_at = ?, task_id = COALESCE(?, task_id)
                WHERE id = ?
                """,
                (now, effective_task_id, session_id),
            )
            self._record_session_event(
                connection,
                session_id=session_id,
                actor=actor,
                event_type="session_heartbeat",
                payload={"status_note": status_note, "task_id": effective_task_id, "files": files or []},
            )
            self._record_activity(connection, actor, "session_heartbeat", effective_task_id, {"session_id": session_id})
            if create_work_log and status_note.strip():
                cursor = connection.execute(
                    """
                    INSERT INTO work_logs (task_id, actor, message, summary, files, created_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        effective_task_id,
                        actor,
                        status_note,
                        "Heartbeat work log",
                        json.dumps(files or []),
                        now,
                    ),
                )
                work_log_id = cursor.lastrowid
                self._touch_session_write(
                    connection,
                    session_id=session_id,
                    actor=actor,
                    event_type="heartbeat_log_work",
                    payload={"task_id": effective_task_id, "message": status_note, "files": files or []},
                )
                connection.commit()
                session_payload = self.get_session(session_id)
                work_log_payload = self._normalize_work_log(
                        self._fetchone_dict(connection, "SELECT * FROM work_logs WHERE id = ?", (work_log_id,)) or {}
                    )
                self._write_session_metadata(session_id, session_payload or {})
                self._append_session_jsonl(
                    session_id,
                    "heartbeat.jsonl",
                    {"time": now, "event": "session_heartbeat", "actor": actor, "task_id": effective_task_id, "status_note": status_note},
                )
                self._append_session_markdown(
                    session_id,
                    "worklog.md",
                    f"- {now} [{actor}] {status_note}",
                    "Session Worklog",
                )
                return {"session": session_payload, "work_log": work_log_payload}
            connection.commit()
        session_payload = self.get_session(session_id)
        self._write_session_metadata(session_id, session_payload or {})
        self._append_session_jsonl(
            session_id,
            "heartbeat.jsonl",
            {"time": now, "event": "session_heartbeat", "actor": actor, "task_id": effective_task_id, "status_note": status_note},
        )
        return session_payload

    def close_session(
        self,
        session_id: str,
        actor: str,
        summary: str = "",
        create_handoff: bool = True,
        existing_handoff_id: int | None = None,
        handoff_summary: str = "",
        handoff_next_steps: str = "",
        handoff_open_questions: str = "",
        handoff_note: str = "",
        handoff_to_actor: str = "next-agent",
    ) -> dict[str, Any] | None:
        now = utc_now()
        with self._connect() as connection:
            session = self._fetchone_dict(connection, "SELECT * FROM sessions WHERE id = ?", (session_id,))
            if not session:
                return None

            handoff_id: int | None = existing_handoff_id
            if create_handoff and not existing_handoff_id and (handoff_summary.strip() or summary.strip()):
                effective_summary = handoff_summary.strip() or summary.strip()
                cursor = connection.execute(
                    """
                    INSERT INTO handoffs (
                        task_id, from_actor, to_actor, summary, next_steps, open_questions, note, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        session.get("task_id"),
                        actor,
                        handoff_to_actor,
                        effective_summary,
                        handoff_next_steps,
                        handoff_open_questions,
                        handoff_note,
                        now,
                        now,
                    ),
                )
                handoff_id = cursor.lastrowid
                connection.execute(
                    """
                    INSERT INTO session_summaries (session_label, summary, actor, created_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (session.get("session_label") or "handoff", effective_summary, actor, now),
                )
            if summary.strip():
                connection.execute(
                    """
                    INSERT INTO session_summaries (session_label, summary, actor, created_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (session.get("session_label") or "session_close", summary.strip(), actor, now),
                )

            connection.execute(
                """
                UPDATE sessions
                SET status = 'closed',
                    closed_at = ?,
                    heartbeat_at = ?,
                    closure_summary = ?,
                    handoff_created = CASE WHEN ? THEN 1 ELSE handoff_created END,
                    last_handoff_at = CASE WHEN ? THEN ? ELSE last_handoff_at END
                WHERE id = ?
                """,
                (
                    now,
                    now,
                    summary,
                    1 if handoff_id else 0,
                    1 if handoff_id else 0,
                    now,
                    session_id,
                ),
            )
            self._record_session_event(
                connection,
                session_id=session_id,
                actor=actor,
                event_type="session_close",
                payload={"summary": summary, "handoff_id": handoff_id},
            )
            self._record_activity(connection, actor, "session_close", session.get("task_id"), {"session_id": session_id})
            connection.commit()
        session_payload = self.get_session(session_id)
        self._write_session_metadata(session_id, session_payload or {})
        self._append_session_jsonl(
            session_id,
            "heartbeat.jsonl",
            {"time": now, "event": "session_close", "actor": actor, "summary": summary, "handoff_id": handoff_id},
        )
        if summary.strip():
            self._append_session_markdown(
                session_id,
                "worklog.md",
                f"- {now} [{actor}] Session closed: {summary.strip()}",
                "Session Worklog",
            )
            if handoff_id:
                handoff = self.get_handoff(handoff_id)
                if handoff:
                    handoff_lines = [
                    "# Handoff",
                    "",
                    f"- ID: {handoff['id']}",
                    f"- From: {handoff['from_actor']}",
                    f"- To: {handoff['to_actor']}",
                    f"- Task: {handoff['task_id'] or 'unassigned'}",
                    f"- Created: {handoff['created_at']}",
                    "",
                    "## Summary",
                    "",
                    handoff["summary"],
                    "",
                    "## Next Steps",
                    "",
                    handoff["next_steps"] or "None recorded.",
                    "",
                    "## Open Questions",
                    "",
                    handoff["open_questions"] or "None recorded.",
                    "",
                    "## Notes",
                    "",
                    handoff["note"] or "None recorded.",
                    "",
                ]
                write_text_atomic(self._session_dir(session_id) / "handoff.md", "\n".join(handoff_lines))
        return session_payload

    def get_session(self, session_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = self._fetchone_dict(connection, "SELECT * FROM sessions WHERE id = ?", (session_id,))
        return self._normalize_session(row)

    def get_active_sessions(self, limit: int = 20, after_heartbeat_at: str | None = None, after_id: str | None = None) -> list[dict[str, Any]]:
        with self._connect() as connection:
            if after_heartbeat_at is not None and after_id is not None:
                rows = self._fetchall_dicts(
                    connection,
                    """
                    SELECT * FROM sessions
                    WHERE status = 'open'
                      AND (heartbeat_at < ? OR (heartbeat_at = ? AND id < ?))
                    ORDER BY heartbeat_at DESC, id DESC
                    LIMIT ?
                    """,
                    (after_heartbeat_at, after_heartbeat_at, after_id, limit),
                )
            elif after_heartbeat_at is not None:
                rows = self._fetchall_dicts(
                    connection,
                    """
                    SELECT * FROM sessions
                    WHERE status = 'open' AND heartbeat_at < ?
                    ORDER BY heartbeat_at DESC
                    LIMIT ?
                    """,
                    (after_heartbeat_at, limit),
                )
            else:
                rows = self._fetchall_dicts(
                    connection,
                    "SELECT * FROM sessions WHERE status = 'open' ORDER BY heartbeat_at DESC LIMIT ?",
                    (limit,),
                )
        return [self._normalize_session(row) for row in rows]

    def upsert_session_env_info(
        self,
        session_id: str,
        ide_name: str = "",
        ide_version: str = "",
        ide_platform: str = "",
        os_name: str = "",
        os_version: str = "",
        env_variables: dict[str, str] | None = None,
        startup_context: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """Store or update IDE/environment metadata for a session."""
        now = utc_now()
        env_json = json.dumps(env_variables or {}, ensure_ascii=True)
        startup_json = json.dumps(startup_context or {}, ensure_ascii=True)
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO session_env_info (
                    session_id, ide_name, ide_version, ide_platform, os_name, os_version,
                    env_variables, startup_context, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(session_id) DO UPDATE SET
                    ide_name = excluded.ide_name,
                    ide_version = excluded.ide_version,
                    ide_platform = excluded.ide_platform,
                    os_name = excluded.os_name,
                    os_version = excluded.os_version,
                    env_variables = excluded.env_variables,
                    startup_context = excluded.startup_context,
                    updated_at = excluded.updated_at
                """,
                (session_id, ide_name, ide_version, ide_platform, os_name, os_version, env_json, startup_json, now, now),
            )
            connection.commit()
        return self.get_session_env_info(session_id)

    def get_session_env_info(self, session_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = self._fetchone_dict(connection, "SELECT * FROM session_env_info WHERE session_id = ?", (session_id,))
        if not row:
            return None
        result = dict(row)
        result["env_variables"] = parse_json(result.get("env_variables") or "{}", {})
        result["startup_context"] = parse_json(result.get("startup_context") or "{}", {})
        return result

    def create_cross_tool_handoff(
        self,
        handoff_id: int,
        target_tool: str,
        target_env: str,
        structured_payload: dict[str, Any],
    ) -> dict[str, Any]:
        """Create a structured cross-tool handoff variant for a specific target environment."""
        now = utc_now()
        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO cross_tool_handoffs (
                    handoff_id, target_tool, target_env, structured_payload, created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (handoff_id, target_tool, target_env, json.dumps(structured_payload, ensure_ascii=True), now),
            )
            cross_id = int(cursor.lastrowid)
            connection.commit()
            return self._fetchone_dict(connection, "SELECT * FROM cross_tool_handoffs WHERE id = ?", (cross_id)) or {}

    def get_cross_tool_handoffs(self, handoff_id: int | None = None, target_tool: str | None = None) -> list[dict[str, Any]]:
        query = "SELECT * FROM cross_tool_handoffs WHERE 1=1"
        params: list[Any] = []
        if handoff_id is not None:
            query += " AND handoff_id = ?"
            params.append(handoff_id)
        if target_tool:
            query += " AND target_tool = ?"
            params.append(target_tool)
        query += " ORDER BY created_at DESC"
        with self._connect() as connection:
            rows = self._fetchall_dicts(connection, query, params)
        return [self._normalize_cross_tool_handoff(row) for row in rows]

    def _normalize_cross_tool_handoff(self, row: dict[str, Any] | None) -> dict[str, Any] | None:
        if not row:
            return None
        result = dict(row)
        result["structured_payload"] = parse_json(result.get("structured_payload") or "{}", {})
        return result

    def upsert_session_lineage(
        self,
        session_id: str,
        parent_session_id: str | None = None,
        continuation_session_id: str | None = None,
        lineage_depth: int = 0,
    ) -> dict[str, Any] | None:
        """Record or update the lineage chain for a session."""
        now = utc_now()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO session_lineage (
                    session_id, parent_session_id, continuation_session_id, lineage_depth, created_at
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(session_id) DO UPDATE SET
                    parent_session_id = COALESCE(excluded.parent_session_id, session_lineage.parent_session_id),
                    continuation_session_id = COALESCE(excluded.continuation_session_id, session_lineage.continuation_session_id),
                    lineage_depth = excluded.lineage_depth
                """,
                (session_id, parent_session_id, continuation_session_id, lineage_depth, now),
            )
            connection.commit()
        return self.get_session_lineage(session_id)

    def get_session_lineage(self, session_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = self._fetchone_dict(connection, "SELECT * FROM session_lineage WHERE session_id = ?", (session_id,))
        return row

    def get_session_lineage_chain(self, session_id: str, max_depth: int = 10) -> list[dict[str, Any]]:
        """Traverse lineage chain from session_id up to max_depth ancestors."""
        chain: list[dict[str, Any]] = []
        current_id: str | None = session_id
        visited: set[str] = set()
        while current_id and len(chain) < max_depth:
            if current_id in visited:
                break
            visited.add(current_id)
            lineage = self.get_session_lineage(current_id)
            if not lineage:
                break
            session = self.get_session(current_id)
            chain.append({
                "session_id": current_id,
                "parent_session_id": lineage.get("parent_session_id"),
                "lineage_depth": lineage.get("lineage_depth", 0),
                "created_at": lineage.get("created_at"),
                "actor": (session or {}).get("actor", "unknown"),
                "status": (session or {}).get("status", "unknown"),
            })
            current_id = lineage.get("parent_session_id")
        return chain

    def detect_missing_writeback(self, include_closed: bool = False) -> list[dict[str, Any]]:
        query = "SELECT * FROM sessions"
        if not include_closed:
            query += " WHERE status = 'open'"
        query += " ORDER BY opened_at DESC"
        now = datetime.now(timezone.utc)
        issues: list[dict[str, Any]] = []
        with self._connect() as connection:
            sessions = self._fetchall_dicts(connection, query)

        for session in sessions:
            heartbeat_at = self._parse_utc(session.get("heartbeat_at"))
            opened_at = self._parse_utc(session.get("opened_at"))
            last_write_at = self._parse_utc(session.get("last_write_at"))
            if heartbeat_at is None or opened_at is None:
                continue
            heartbeat_age = int((now - heartbeat_at).total_seconds())
            open_age = int((now - opened_at).total_seconds())
            write_age = int((now - last_write_at).total_seconds()) if last_write_at else None

            if session["status"] == "open" and session["require_heartbeat"] and heartbeat_age > session["heartbeat_interval_seconds"]:
                issues.append(
                    {
                        "session_id": session["id"],
                        "issue": "heartbeat_overdue",
                        "severity": "high",
                        "details": f"No heartbeat for {heartbeat_age} seconds.",
                        "recommended_action": "Call session_heartbeat immediately.",
                    }
                )

            if session["require_work_log"]:
                if session["write_count"] < session["min_work_logs"] and open_age > session["work_log_interval_seconds"]:
                    issues.append(
                        {
                            "session_id": session["id"],
                            "issue": "missing_initial_writeback",
                            "severity": "high",
                            "details": "Session has stayed open beyond the work-log interval without required write-back.",
                            "recommended_action": "Call log_work or a structured write tool with the session_id.",
                        }
                    )
                elif last_write_at and write_age is not None and write_age > session["work_log_interval_seconds"]:
                    issues.append(
                        {
                            "session_id": session["id"],
                            "issue": "writeback_overdue",
                            "severity": "medium",
                            "details": f"Last write-back was {write_age} seconds ago.",
                            "recommended_action": "Write a progress log, decision, blocker, or task update.",
                        }
                    )

            stale_threshold = max(session["heartbeat_interval_seconds"] * 2, 1800)
            abandoned_threshold = max(session["heartbeat_interval_seconds"] * 4, 7200)
            if session["status"] == "open" and heartbeat_age > stale_threshold:
                issues.append(
                    {
                        "session_id": session["id"],
                        "issue": "stale_open_session",
                        "severity": "high" if heartbeat_age > abandoned_threshold else "medium",
                        "details": f"Session has been idle for {heartbeat_age} seconds without a new heartbeat.",
                        "recommended_action": "Resume the session or create a recovery handoff before continuing in another tool.",
                    }
                )
            if session["status"] == "open" and heartbeat_age > abandoned_threshold:
                issues.append(
                    {
                        "session_id": session["id"],
                        "issue": "abandoned_session",
                        "severity": "high",
                        "details": "Session appears abandoned and should be recovered before more work continues elsewhere.",
                        "recommended_action": "Call recover_session to generate an emergency handoff and resume packet.",
                    }
                )

            if session["status"] == "closed" and session["handoff_required"] and not session["handoff_created"]:
                issues.append(
                    {
                        "session_id": session["id"],
                        "issue": "missing_handoff_on_close",
                        "severity": "high",
                        "details": "Session was closed without a recorded handoff.",
                        "recommended_action": "Create a handoff or reopen the session and close it with a handoff.",
                    }
                )

        normalized_issues: list[dict[str, Any]] = []
        for issue in issues:
            session = self.get_session(issue["session_id"]) or {}
            normalized_issues.append(
                {
                    **issue,
                    "session_label": session.get("session_label", issue["session_id"]),
                    "workstream_key": session.get("workstream_key", ""),
                    "workstream_title": session.get("workstream_title", session.get("session_label", issue["session_id"])),
                    "task_id": session.get("task_id"),
                }
            )
        return normalized_issues

    def get_relevant_files(self, task_id: str | None = None, limit: int = 20) -> list[str]:
        task = self.get_task(task_id) if task_id else self.get_current_task()
        seen: list[str] = []
        if task:
            for file_path in task.get("relevant_files", []):
                if file_path not in seen:
                    seen.append(file_path)

        for work_log in self.get_recent_work(limit=limit):
            if task and work_log.get("task_id") not in {None, task["id"]}:
                continue
            for file_path in work_log.get("files", []):
                if file_path not in seen:
                    seen.append(file_path)
            if len(seen) >= limit:
                break
        for event in self.list_command_events(limit=limit, task_id=task["id"] if task else task_id):
            for file_path in event.get("files_changed", []):
                if file_path not in seen:
                    seen.append(file_path)
                if len(seen) >= limit:
                    break
            if len(seen) >= limit:
                break
        return seen[:limit]

    def get_table_schema(self, table_name: str) -> dict[str, Any]:
        if not re.match(r"^[A-Za-z0-9_]+$", table_name):
            raise ValueError("Invalid table name.")
        with self._connect() as connection:
            rows = self._fetchall_dicts(connection, f"PRAGMA table_info({table_name})")
        return {"table": table_name, "columns": rows}

    def get_project_status_snapshot(self) -> dict[str, Any]:
        current_task = self.get_current_task()
        current_task_id = current_task["id"] if current_task else None
        return {
            "app_name": self.config.app_name,
            "project_path": self.project_root,
            "current_task": current_task,
            "active_tasks": self.get_active_tasks(limit=10),
            "blockers": self.get_blockers(open_only=True, limit=10),
            "recent_work": self.get_recent_work(limit=8),
            "recent_checkpoints": self.get_recent_checkpoints(limit=self.config.checkpoints.render_limit, task_id=current_task_id),
            "current_task_progress": self.get_checkpoint_progress(current_task_id) if current_task_id else None,
            "latest_handoff": self.get_latest_handoff(),
            "decisions": self.get_decisions(limit=8),
            "relevant_files": self.get_relevant_files(current_task_id),
            "recent_commands": self.list_command_events(limit=8, task_id=current_task_id),
            "active_sessions": self.get_active_sessions(limit=10),
            "session_audit": self.detect_missing_writeback(),
        }

    def get_context_state_version(self, include_semantic: bool = False) -> str:
        with self._connect() as connection:
            parts = [
                ("tasks", "SELECT COUNT(*) AS count, COALESCE(MAX(updated_at), '') AS ts FROM tasks"),
                ("work_logs", "SELECT COUNT(*) AS count, COALESCE(MAX(created_at), '') AS ts FROM work_logs"),
                ("decisions", "SELECT COUNT(*) AS count, COALESCE(MAX(created_at), '') AS ts FROM decisions"),
                ("blockers", "SELECT COUNT(*) AS count, COALESCE(MAX(COALESCE(resolved_at, created_at)), '') AS ts FROM blockers"),
                ("handoffs", "SELECT COUNT(*) AS count, COALESCE(MAX(updated_at), '') AS ts FROM handoffs"),
                ("sessions", "SELECT COUNT(*) AS count, COALESCE(MAX(COALESCE(closed_at, heartbeat_at)), '') AS ts FROM sessions"),
                ("project_brief_sections", "SELECT COUNT(*) AS count, COALESCE(MAX(updated_at), '') AS ts FROM project_brief_sections"),
                ("daily_entries", "SELECT COUNT(*) AS count, COALESCE(MAX(created_at), '') AS ts FROM daily_entries"),
                ("checkpoints", "SELECT COUNT(*) AS count, COALESCE(MAX(created_at), '') AS ts FROM checkpoints"),
                ("project_state", "SELECT COUNT(*) AS count, COALESCE(MAX(updated_at), '') AS ts FROM project_state"),
                ("command_events", "SELECT COUNT(*) AS count, COALESCE(MAX(created_at), '') AS ts FROM command_events"),
            ]
            if include_semantic:
                parts.append(("semantic_descriptions", "SELECT COUNT(*) AS count, COALESCE(MAX(verified_at), '') AS ts FROM semantic_descriptions"))
                parts.append(("semantic_symbol_index", "SELECT COUNT(*) AS count, COALESCE(MAX(updated_at), '') AS ts FROM semantic_symbol_index"))
            tokens: list[str] = []
            for label, query in parts:
                row = self._fetchone_dict(connection, query) or {}
                tokens.append(f"{label}:{row.get('count', 0)}:{row.get('ts', '')}")
        return "|".join(tokens)

    def get_context_artifact(self, artifact_type: str, scope_key: str, params_signature: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = self._fetchone_dict(
                connection,
                """
                SELECT * FROM context_artifacts
                WHERE artifact_type = ? AND scope_key = ? AND params_signature = ?
                """,
                (artifact_type, scope_key, params_signature),
            )
        return self._normalize_context_artifact(row)

    def upsert_context_artifact(
        self,
        *,
        artifact_key: str,
        artifact_type: str,
        scope_key: str,
        params_signature: str,
        state_version: str,
        content: str,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        generated_at = utc_now()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO context_artifacts (
                    artifact_key, artifact_type, scope_key, params_signature,
                    state_version, content, metadata, generated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(artifact_key) DO UPDATE SET
                    artifact_type=excluded.artifact_type,
                    scope_key=excluded.scope_key,
                    params_signature=excluded.params_signature,
                    state_version=excluded.state_version,
                    content=excluded.content,
                    metadata=excluded.metadata,
                    generated_at=excluded.generated_at
                """,
                (
                    artifact_key,
                    artifact_type,
                    scope_key,
                    params_signature,
                    state_version,
                    content,
                    json.dumps(metadata or {}, ensure_ascii=True),
                    generated_at,
                ),
            )
            connection.commit()
        return self.get_context_artifact(artifact_type, scope_key, params_signature)

    def get_context_chunk(
        self,
        scope_key: str,
        params_signature: str,
        chunk_index: int,
    ) -> dict[str, Any] | None:
        """Retrieve a specific chunk of a context artifact by composite key."""
        chunk_key = f"{scope_key}:{params_signature}:chunk:{chunk_index}"
        with self._connect() as connection:
            row = self._fetchone_dict(
                connection,
                "SELECT * FROM context_artifacts WHERE artifact_key = ?",
                (f"context_chunk:{chunk_key}",),
            )
        return self._normalize_context_artifact(row)

    def upsert_context_chunk(
        self,
        *,
        scope_key: str,
        params_signature: str,
        chunk_index: int,
        total_chunks: int,
        state_version: str,
        content: str,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """Store a context chunk artifact."""
        chunk_key = f"{scope_key}:{params_signature}:chunk:{chunk_index}"
        generated_at = utc_now()
        meta = dict(metadata or {})
        meta["chunk_index"] = chunk_index
        meta["total_chunks"] = total_chunks
        meta["is_last"] = chunk_index == total_chunks - 1
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO context_artifacts (
                    artifact_key, artifact_type, scope_key, params_signature,
                    state_version, content, metadata, generated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(artifact_key) DO UPDATE SET
                    state_version=excluded.state_version,
                    content=excluded.content,
                    metadata=excluded.metadata,
                    generated_at=excluded.generated_at
                """,
                (
                    f"context_chunk:{chunk_key}",
                    "context_chunk",
                    scope_key,
                    params_signature,
                    state_version,
                    content,
                    json.dumps(meta, ensure_ascii=True),
                    generated_at,
                ),
            )
            connection.commit()
        return self.get_context_chunk(scope_key, params_signature, chunk_index)

    def record_token_usage_event(
        self,
        *,
        event_type: str,
        operation: str,
        actor: str = "system",
        session_id: str | None = None,
        task_id: str | None = None,
        model_name: str = "",
        provider: str = "",
        client_name: str = "",
        raw_input_tokens: int = 0,
        raw_output_tokens: int = 0,
        estimated_input_tokens: int = 0,
        estimated_output_tokens: int = 0,
        compact_input_tokens: int = 0,
        compact_output_tokens: int = 0,
        saved_tokens: int = 0,
        cache_creation_input_tokens: int = 0,
        cache_read_input_tokens: int = 0,
        raw_chars: int = 0,
        compact_chars: int = 0,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        created_at = utc_now()
        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO token_usage_events (
                    event_type, operation, actor, session_id, task_id,
                    model_name, provider, client_name,
                    raw_input_tokens, raw_output_tokens,
                    estimated_input_tokens, estimated_output_tokens,
                    compact_input_tokens, compact_output_tokens, saved_tokens,
                    cache_creation_input_tokens, cache_read_input_tokens,
                    raw_chars, compact_chars, metadata, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event_type,
                    operation,
                    actor,
                    session_id,
                    task_id,
                    model_name,
                    provider,
                    client_name,
                    max(int(raw_input_tokens or 0), 0),
                    max(int(raw_output_tokens or 0), 0),
                    max(int(estimated_input_tokens or 0), 0),
                    max(int(estimated_output_tokens or 0), 0),
                    max(int(compact_input_tokens or 0), 0),
                    max(int(compact_output_tokens or 0), 0),
                    max(int(saved_tokens or 0), 0),
                    max(int(cache_creation_input_tokens or 0), 0),
                    max(int(cache_read_input_tokens or 0), 0),
                    max(int(raw_chars or 0), 0),
                    max(int(compact_chars or 0), 0),
                    json.dumps(metadata or {}, ensure_ascii=True),
                    created_at,
                ),
            )
            connection.commit()
            row = self._fetchone_dict(connection, "SELECT * FROM token_usage_events WHERE id = ?", (cursor.lastrowid,))
        return self._normalize_token_usage_event(row)

    def list_token_usage_events(
        self,
        *,
        limit: int = 50,
        operation: str | None = None,
        event_type: str | None = None,
        session_id: str | None = None,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if operation:
            clauses.append("operation = ?")
            params.append(operation)
        if event_type:
            clauses.append("event_type = ?")
            params.append(event_type)
        if session_id:
            clauses.append("session_id = ?")
            params.append(session_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._connect() as connection:
            rows = self._fetchall_dicts(
                connection,
                f"""
                SELECT * FROM token_usage_events
                {where}
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (*params, limit),
            )
        return [self._normalize_token_usage_event(row) for row in rows]

    def get_token_usage_stats(
        self,
        *,
        limit: int = 200,
        operation: str | None = None,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        events = self.list_token_usage_events(limit=limit, operation=operation, session_id=session_id)
        totals = {
            "raw_input_tokens": 0,
            "raw_output_tokens": 0,
            "estimated_input_tokens": 0,
            "estimated_output_tokens": 0,
            "compact_input_tokens": 0,
            "compact_output_tokens": 0,
            "saved_tokens": 0,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
            "raw_chars": 0,
            "compact_chars": 0,
        }
        by_operation: dict[str, dict[str, Any]] = {}
        for event in events:
            for key in totals:
                totals[key] += int(event.get(key) or 0)
            op = event.get("operation", "unknown")
            entry = by_operation.setdefault(
                op,
                {
                    "operation": op,
                    "event_count": 0,
                    "saved_tokens": 0,
                    "compact_output_tokens": 0,
                    "estimated_output_tokens": 0,
                },
            )
            entry["event_count"] += 1
            entry["saved_tokens"] += int(event.get("saved_tokens") or 0)
            entry["compact_output_tokens"] += int(event.get("compact_output_tokens") or 0)
            entry["estimated_output_tokens"] += int(event.get("estimated_output_tokens") or 0)
        return {
            "event_count": len(events),
            "limit": limit,
            "filters": {
                "operation": operation,
                "session_id": session_id,
            },
            "totals": totals,
            "by_operation": sorted(by_operation.values(), key=lambda item: (-item["saved_tokens"], item["operation"])),
            "recent_events": events[: min(10, len(events))],
        }

    def create_raw_output_capture(
        self,
        *,
        capture_id: str,
        actor: str,
        command_text: str,
        profile: str,
        reason: str,
        output_path: str,
        preview: str = "",
        session_id: str | None = None,
        task_id: str | None = None,
        exit_code: int = 0,
        raw_chars: int = 0,
        raw_tokens_est: int = 0,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        created_at = utc_now()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO raw_output_captures (
                    capture_id, session_id, task_id, actor, command_text, profile,
                    reason, exit_code, output_path, preview, raw_chars, raw_tokens_est,
                    metadata, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    capture_id,
                    session_id,
                    task_id,
                    actor,
                    command_text,
                    profile,
                    reason,
                    int(exit_code or 0),
                    output_path,
                    preview,
                    max(int(raw_chars or 0), 0),
                    max(int(raw_tokens_est or 0), 0),
                    json.dumps(metadata or {}, ensure_ascii=True),
                    created_at,
                ),
            )
            connection.commit()
        return self.get_raw_output_capture(capture_id)

    def get_raw_output_capture(self, capture_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = self._fetchone_dict(connection, "SELECT * FROM raw_output_captures WHERE capture_id = ?", (capture_id,))
        return self._normalize_raw_output_capture(row)

    def record_command_event(
        self,
        *,
        actor: str,
        command_text: str,
        cwd: str = "",
        event_kind: str = "completed",
        status: str = "completed",
        risk_level: str = "normal",
        exit_code: int = 0,
        duration_ms: int = 0,
        summary: str = "",
        stdout_summary: str = "",
        stderr_summary: str = "",
        output_profile: str = "",
        raw_capture_id: str | None = None,
        raw_output_available: bool = False,
        files_changed: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        session_id: str | None = None,
        task_id: str | None = None,
    ) -> dict[str, Any] | None:
        created_at = utc_now()
        normalized_files = [
            self._normalize_project_file_path(path)
            for path in (files_changed or [])
            if isinstance(path, str) and path.strip()
        ]
        normalized_cwd = self._normalize_project_file_path(cwd) if cwd else ""
        payload = {
            "command_text": command_text,
            "status": status,
            "exit_code": int(exit_code or 0),
            "duration_ms": max(int(duration_ms or 0), 0),
            "raw_capture_id": raw_capture_id,
            "files_changed": normalized_files,
        }
        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO command_events (
                    session_id, task_id, actor, command_text, cwd, event_kind, status,
                    risk_level, exit_code, duration_ms, summary, stdout_summary, stderr_summary,
                    output_profile, raw_capture_id, raw_output_available, files_changed, metadata, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    task_id,
                    actor,
                    command_text,
                    normalized_cwd,
                    event_kind or "completed",
                    status or "completed",
                    risk_level or "normal",
                    int(exit_code or 0),
                    max(int(duration_ms or 0), 0),
                    summary or "",
                    stdout_summary or "",
                    stderr_summary or "",
                    output_profile or "",
                    raw_capture_id,
                    1 if raw_output_available else 0,
                    json.dumps(normalized_files, ensure_ascii=True),
                    json.dumps(metadata or {}, ensure_ascii=True),
                    created_at,
                ),
            )
            event_id = cursor.lastrowid
            self._record_activity(connection, actor, "record_command_event", task_id, payload)
            self._touch_session_write(
                connection,
                session_id=session_id,
                actor=actor,
                event_type="record_command_event",
                payload={"task_id": task_id, **payload},
            )
            connection.commit()
            row = self._fetchone_dict(connection, "SELECT * FROM command_events WHERE id = ?", (event_id,))
        if session_id:
            self._append_session_markdown(
                session_id,
                "commands.md",
                f"- {created_at} [{status}] `{command_text}`"
                + (f" (exit {int(exit_code or 0)})" if int(exit_code or 0) else "")
                + (f"\n  - Summary: {summary}" if summary else ""),
                "Session Commands",
            )
        return self._normalize_command_event(row)

    def get_command_event(self, event_id: int) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = self._fetchone_dict(connection, "SELECT * FROM command_events WHERE id = ?", (event_id,))
        return self._normalize_command_event(row)

    def list_command_events(
        self,
        *,
        limit: int = 20,
        after_id: int | None = None,
        session_id: str | None = None,
        task_id: str | None = None,
        status: str | None = None,
        actor: str | None = None,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if session_id:
            clauses.append("session_id = ?")
            params.append(session_id)
        if task_id:
            clauses.append("task_id = ?")
            params.append(task_id)
        if status:
            clauses.append("status = ?")
            params.append(status)
        if actor:
            clauses.append("actor = ?")
            params.append(actor)
        if after_id is not None:
            clauses.append("id < ?")
            params.append(after_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._connect() as connection:
            rows = self._fetchall_dicts(
                connection,
                f"""
                SELECT * FROM command_events
                {where}
                ORDER BY created_at DESC, id DESC
                LIMIT ?
                """,
                (*params, limit),
            )
        return [self._normalize_command_event(row) for row in rows]

    def get_last_command_result(
        self,
        *,
        session_id: str | None = None,
        task_id: str | None = None,
        actor: str | None = None,
    ) -> dict[str, Any] | None:
        events = self.list_command_events(limit=1, session_id=session_id, task_id=task_id, actor=actor)
        return events[0] if events else None

    def get_command_failures(
        self,
        *,
        limit: int = 20,
        session_id: str | None = None,
        task_id: str | None = None,
        actor: str | None = None,
    ) -> list[dict[str, Any]]:
        with self._connect() as connection:
            clauses = ["(status = 'failed' OR exit_code <> 0)"]
            params: list[Any] = []
            if session_id:
                clauses.append("session_id = ?")
                params.append(session_id)
            if task_id:
                clauses.append("task_id = ?")
                params.append(task_id)
            if actor:
                clauses.append("actor = ?")
                params.append(actor)
            rows = self._fetchall_dicts(
                connection,
                f"""
                SELECT * FROM command_events
                WHERE {' AND '.join(clauses)}
                ORDER BY created_at DESC, id DESC
                LIMIT ?
                """,
                (*params, limit),
            )
        return [self._normalize_command_event(row) for row in rows]

    def get_command_events_since(self, since_timestamp: str, limit: int = 20) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = self._fetchall_dicts(
                connection,
                """
                SELECT * FROM command_events
                WHERE created_at >= ?
                ORDER BY created_at DESC, id DESC
                LIMIT ?
                """,
                (since_timestamp, limit),
            )
        return [self._normalize_command_event(row) for row in rows]

    def get_file_fingerprints(self) -> dict[str, dict[str, Any]]:
        """Return current file fingerprints keyed by file path."""
        with self._connect() as connection:
            rows = self._fetchall_dicts(connection, "SELECT * FROM semantic_file_fingerprints")
        return {row["file_path"]: dict(row) for row in rows}

    def is_context_stale(self, cached_artifact: dict[str, Any]) -> bool:
        """Check if a cached context artifact is stale based on file fingerprint changes.

        Compares the fingerprint set stored on the artifact against the current
        file fingerprints. Returns True if any tracked file has changed.
        Returns False if there is no fingerprint data to compare against (empty table)
        so that the state_version check is the authoritative staleness signal.
        """
        meta = cached_artifact.get("metadata") or {}
        stored_fingerprints: dict[str, str] = meta.get("fingerprint_set") or {}
        if not stored_fingerprints:
            return False
        current = self.get_file_fingerprints()
        if not current:
            return False
        for file_path, stored_fingerprint in stored_fingerprints.items():
            current_entry = current.get(file_path)
            if not current_entry:
                return True
            if current_entry.get("fingerprint") != stored_fingerprint:
                return True
        return False

    def get_recent_work_since(self, since_timestamp: str, limit: int = 20) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = self._fetchall_dicts(
                connection,
                """
                SELECT * FROM work_logs
                WHERE created_at >= ?
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (since_timestamp, limit),
            )
        return [self._normalize_work_log(row) for row in rows]

    def get_decisions_since(self, since_timestamp: str, limit: int = 20) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = self._fetchall_dicts(
                connection,
                """
                SELECT * FROM decisions
                WHERE created_at >= ?
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (since_timestamp, limit),
            )
        return rows

    def get_blockers_since(self, since_timestamp: str, limit: int = 20) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = self._fetchall_dicts(
                connection,
                """
                SELECT * FROM blockers
                WHERE created_at >= ? OR COALESCE(resolved_at, '') >= ?
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (since_timestamp, since_timestamp, limit),
            )
        return rows

    def get_handoffs_since(self, since_timestamp: str, limit: int = 10) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = self._fetchall_dicts(
                connection,
                """
                SELECT * FROM handoffs
                WHERE created_at >= ?
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (since_timestamp, limit),
            )
        return rows

    def get_tasks_updated_since(self, since_timestamp: str, limit: int = 20) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = self._fetchall_dicts(
                connection,
                """
                SELECT * FROM tasks
                WHERE updated_at >= ? OR created_at >= ?
                ORDER BY updated_at DESC
                LIMIT ?
                """,
                (since_timestamp, since_timestamp, limit),
            )
        return [self._normalize_task(row) for row in rows]

    # =========================================================================
    # Semantic Knowledge
    # =========================================================================

    def replace_semantic_index(self, entities: list[dict[str, Any]], file_fingerprints: list[dict[str, Any]]) -> dict[str, Any]:
        with self._connect() as connection:
            previous_rows = self._fetchall_dicts(connection, "SELECT file_path, fingerprint FROM semantic_file_fingerprints")
            previous = {row["file_path"]: row["fingerprint"] for row in previous_rows}

            connection.execute("DELETE FROM semantic_symbol_index")
            connection.execute("DELETE FROM semantic_file_fingerprints")

            for item in file_fingerprints:
                connection.execute(
                    """
                    INSERT INTO semantic_file_fingerprints (file_path, fingerprint, file_size, modified_at, scanned_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (item["file_path"], item["fingerprint"], item["file_size"], item["modified_at"], item["scanned_at"]),
                )

            seen_entity_keys: set[str] = set()
            for entity in entities:
                entity_key = entity["entity_key"]
                if entity_key in seen_entity_keys:
                    continue
                seen_entity_keys.add(entity_key)
                connection.execute(
                    """
                    INSERT INTO semantic_symbol_index (
                        entity_key, entity_type, name, file_path, symbol_path, signature, line_number,
                        feature_tags, source_files, source_fingerprint, summary_hint, metadata, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        entity_key,
                        entity["entity_type"],
                        entity["name"],
                        entity["file_path"],
                        entity["symbol_path"],
                        entity["signature"],
                        entity["line_number"],
                        json.dumps(entity.get("feature_tags", [])),
                        json.dumps(entity.get("source_files", [])),
                        entity["source_fingerprint"],
                        entity.get("summary_hint", ""),
                        json.dumps(entity.get("metadata", {})),
                        entity["updated_at"],
                    ),
                )

            current_rows = self._fetchall_dicts(connection, "SELECT entity_key, source_fingerprint FROM semantic_symbol_index")
            current_fingerprints = {row["entity_key"]: row["source_fingerprint"] for row in current_rows}
            description_rows = self._fetchall_dicts(connection, "SELECT entity_key, source_fingerprint FROM semantic_descriptions")
            stale_count = 0
            for row in description_rows:
                current_fingerprint = current_fingerprints.get(row["entity_key"])
                if current_fingerprint != row["source_fingerprint"]:
                    connection.execute("UPDATE semantic_descriptions SET stale = 1 WHERE entity_key = ?", (row["entity_key"],))
                    stale_count += 1
            connection.commit()

        changed_files = [path for path, fingerprint in previous.items() if any(item["file_path"] == path and item["fingerprint"] != fingerprint for item in file_fingerprints)]
        added_files = [item["file_path"] for item in file_fingerprints if item["file_path"] not in previous]
        return {
            "entity_count": len(entities),
            "file_count": len(file_fingerprints),
            "changed_files": sorted(changed_files),
            "added_files": sorted(added_files),
            "stale_descriptions": stale_count,
        }

    def get_symbol_index_entry(self, entity_key: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = self._fetchone_dict(connection, "SELECT * FROM semantic_symbol_index WHERE entity_key = ?", (entity_key,))
        return self._normalize_semantic_index_row(row)

    def get_module_index(self, module_path: str) -> dict[str, Any] | None:
        normalized = self._normalize_project_file_path(module_path)
        with self._connect() as connection:
            row = self._fetchone_dict(
                connection,
                "SELECT * FROM semantic_symbol_index WHERE entity_type = 'module' AND file_path = ?",
                (normalized,),
            )
        return self._normalize_semantic_index_row(row)

    def get_feature_index(self, feature_name: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = self._fetchone_dict(
                connection,
                "SELECT * FROM semantic_symbol_index WHERE entity_type = 'feature' AND lower(name) = lower(?)",
                (feature_name,),
            )
        return self._normalize_semantic_index_row(row)

    def get_symbol_candidates(self, symbol_name: str, module_path: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
        query = "SELECT * FROM semantic_symbol_index WHERE entity_type IN ('function', 'class') AND name = ?"
        params: list[Any] = [symbol_name]
        if module_path:
            query += " AND file_path = ?"
            params.append(self._normalize_project_file_path(module_path))
        query += " ORDER BY file_path ASC, line_number ASC LIMIT ?"
        params.append(limit)
        with self._connect() as connection:
            rows = self._fetchall_dicts(connection, query, tuple(params))
        return [self._normalize_semantic_index_row(row) for row in rows]

    def search_semantic_index(self, query: str, limit: int = 10) -> list[dict[str, Any]]:
        like = f"%{query.lower()}%"
        with self._connect() as connection:
            rows = self._fetchall_dicts(
                connection,
                """
                SELECT * FROM semantic_symbol_index
                WHERE lower(name) LIKE ? OR lower(file_path) LIKE ? OR lower(symbol_path) LIKE ? OR lower(summary_hint) LIKE ?
                ORDER BY
                    CASE
                        WHEN lower(name) = lower(?) THEN 0
                        WHEN lower(file_path) = lower(?) THEN 1
                        ELSE 2
                    END,
                    entity_type ASC,
                    file_path ASC
                LIMIT ?
                """,
                (like, like, like, like, query, query, limit),
            )
        return [self._normalize_semantic_index_row(row) for row in rows]

    def get_related_symbols(self, entity_key: str, limit: int = 8) -> list[dict[str, Any]]:
        origin = self.get_symbol_index_entry(entity_key)
        if not origin:
            return []
        with self._connect() as connection:
            same_file = self._fetchall_dicts(
                connection,
                """
                SELECT * FROM semantic_symbol_index
                WHERE entity_key != ? AND file_path = ? AND entity_type IN ('module', 'function', 'class')
                ORDER BY entity_type ASC, line_number ASC
                LIMIT ?
                """,
                (entity_key, origin["file_path"], limit),
            )
        rows = [self._normalize_semantic_index_row(row) for row in same_file]
        if len(rows) >= limit:
            return rows[:limit]
        feature_tags = origin.get("feature_tags", [])
        if not feature_tags:
            return rows[:limit]
        with self._connect() as connection:
            others = self._fetchall_dicts(
                connection,
                "SELECT * FROM semantic_symbol_index WHERE entity_key != ? ORDER BY updated_at DESC",
                (entity_key,),
            )
        seen = {row["entity_key"] for row in rows}
        for row in others:
            normalized = self._normalize_semantic_index_row(row)
            if normalized["entity_key"] in seen:
                continue
            if set(normalized.get("feature_tags", [])) & set(feature_tags):
                rows.append(normalized)
                seen.add(normalized["entity_key"])
            if len(rows) >= limit:
                break
        return rows[:limit]

    def get_symbol_index_stats(self) -> dict[str, Any]:
        with self._connect() as connection:
            counts = self._fetchall_dicts(
                connection,
                "SELECT entity_type, COUNT(*) AS count FROM semantic_symbol_index GROUP BY entity_type ORDER BY entity_type ASC",
            )
            file_count_row = self._fetchone_dict(connection, "SELECT COUNT(*) AS count FROM semantic_file_fingerprints")
        return {
            "entity_counts": {row["entity_type"]: row["count"] for row in counts},
            "tracked_files": (file_count_row or {}).get("count", 0),
        }

    def upsert_semantic_description(self, description: dict[str, Any]) -> dict[str, Any]:
        now = utc_now()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO semantic_descriptions (
                    entity_key, entity_type, name, file_path, symbol_path, signature,
                    purpose, why_it_exists, how_it_is_used, inputs_outputs, side_effects, risks,
                    related_files, related_decisions, related_tasks, related_symbols,
                    source_fingerprint, generated_at, verified_at, stale, metadata,
                    llm_model, llm_latency_ms, llm_input_tokens, llm_output_tokens, llm_generated,
                    language
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(entity_key) DO UPDATE SET
                    entity_type=excluded.entity_type,
                    name=excluded.name,
                    file_path=excluded.file_path,
                    symbol_path=excluded.symbol_path,
                    signature=excluded.signature,
                    purpose=excluded.purpose,
                    why_it_exists=excluded.why_it_exists,
                    how_it_is_used=excluded.how_it_is_used,
                    inputs_outputs=excluded.inputs_outputs,
                    side_effects=excluded.side_effects,
                    risks=excluded.risks,
                    related_files=excluded.related_files,
                    related_decisions=excluded.related_decisions,
                    related_tasks=excluded.related_tasks,
                    related_symbols=excluded.related_symbols,
                    source_fingerprint=excluded.source_fingerprint,
                    generated_at=excluded.generated_at,
                    verified_at=excluded.verified_at,
                    stale=excluded.stale,
                    metadata=excluded.metadata,
                    llm_model=excluded.llm_model,
                    llm_latency_ms=excluded.llm_latency_ms,
                    llm_input_tokens=excluded.llm_input_tokens,
                    llm_output_tokens=excluded.llm_output_tokens,
                    llm_generated=excluded.llm_generated,
                    language=excluded.language
                """,
                (
                    description["entity_key"],
                    description["entity_type"],
                    description["name"],
                    description["file"],
                    description.get("symbol_path", description["name"]),
                    description.get("signature", ""),
                    description["purpose"],
                    description["why_it_exists"],
                    description["how_it_is_used"],
                    description["inputs_outputs"],
                    description["side_effects"],
                    description["risks"],
                    json.dumps(description.get("related_files", [])),
                    json.dumps(description.get("related_decisions", [])),
                    json.dumps(description.get("related_tasks", [])),
                    json.dumps(description.get("related_symbols", [])),
                    description["source_fingerprint"],
                    description.get("generated_at", now),
                    description.get("verified_at", now),
                    0 if description.get("freshness", "fresh") == "fresh" else 1,
                    json.dumps(description.get("metadata", {})),
                    description.get("llm_model"),
                    description.get("llm_latency_ms"),
                    description.get("llm_input_tokens"),
                    description.get("llm_output_tokens"),
                    description.get("llm_model") is not None,
                    description.get("language"),
                ),
            )
            connection.commit()
        return self.get_semantic_description(description["entity_key"]) or {}

    def get_semantic_description(self, entity_key: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = self._fetchone_dict(connection, "SELECT * FROM semantic_descriptions WHERE entity_key = ?", (entity_key,))
        return self._normalize_semantic_description(row)

    def get_cached_semantic_descriptions(
        self,
        entity_type: str | None = None,
        fresh_only: bool = False,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        conditions: list[str] = []
        params: list[Any] = []
        if entity_type:
            conditions.append("entity_type = ?")
            params.append(entity_type)
        if fresh_only:
            conditions.append("stale = 0")
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        with self._connect() as connection:
            rows = self._fetchall_dicts(
                connection,
                f"SELECT * FROM semantic_descriptions {where} ORDER BY verified_at DESC LIMIT ?",
                tuple([*params, limit]),
            )
        return [self._normalize_semantic_description(row) for row in rows]

    def invalidate_semantic_cache(
        self,
        entity_key: str | None = None,
        file_paths: list[str] | None = None,
    ) -> dict[str, Any]:
        normalized_paths = [self._normalize_project_file_path(item) for item in (file_paths or [])]
        with self._connect() as connection:
            if entity_key:
                cursor = connection.execute("UPDATE semantic_descriptions SET stale = 1 WHERE entity_key = ?", (entity_key,))
            elif normalized_paths:
                placeholders = ",".join("?" * len(normalized_paths))
                cursor = connection.execute(
                    f"UPDATE semantic_descriptions SET stale = 1 WHERE file_path IN ({placeholders})",
                    tuple(normalized_paths),
                )
            else:
                cursor = connection.execute("UPDATE semantic_descriptions SET stale = 1")
            connection.commit()
        return {"invalidated": cursor.rowcount, "entity_key": entity_key, "file_paths": normalized_paths}

    def get_tasks_for_files(self, file_paths: list[str], limit: int = 10) -> list[dict[str, Any]]:
        normalized = {self._normalize_project_file_path(item) for item in file_paths if item}
        if not normalized:
            return []
        matches: list[dict[str, Any]] = []
        for task in self.get_active_tasks(limit=200):
            task_files = {self._normalize_project_file_path(item) for item in task.get("relevant_files", [])}
            if task_files & normalized:
                matches.append(task)
            if len(matches) >= limit:
                break
        return matches[:limit]

    def get_related_decisions_for_files(self, file_paths: list[str], limit: int = 10) -> list[dict[str, Any]]:
        tasks = self.get_tasks_for_files(file_paths, limit=20)
        task_ids = {item["id"] for item in tasks}
        decisions = self.get_decisions(limit=100)
        related = [item for item in decisions if item.get("task_id") in task_ids]
        return related[:limit]

    def get_recent_file_activity(self, limit: int = 10) -> list[str]:
        seen: list[str] = []
        for row in self.get_recent_work(limit=100):
            for file_path in row.get("files", []):
                normalized = self._normalize_project_file_path(file_path)
                if normalized not in seen:
                    seen.append(normalized)
                if len(seen) >= limit:
                    return seen
        return seen

    def create_scan_job(
        self,
        job_id: str,
        *,
        job_type: str,
        project_path: str,
        requested_by: str,
        force_refresh: bool,
    ) -> dict[str, Any]:
        now = utc_now()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO scan_jobs (
                    id, job_type, project_path, status, requested_by, force_refresh,
                    requested_at, started_at, finished_at, progress_message, result_json, error_text
                )
                VALUES (?, ?, ?, 'queued', ?, ?, ?, NULL, NULL, '', '{}', '')
                """,
                (job_id, job_type, project_path, requested_by, int(force_refresh), now),
            )
            connection.commit()
            row = self._fetchone_dict(connection, "SELECT * FROM scan_jobs WHERE id = ?", (job_id,))
        return self._normalize_scan_job(row) or {}

    def update_scan_job(
        self,
        job_id: str,
        *,
        status: str | None = None,
        started_at: str | None = None,
        finished_at: str | None = None,
        progress_message: str | None = None,
        result: dict[str, Any] | None = None,
        error_text: str | None = None,
    ) -> dict[str, Any] | None:
        updates: list[str] = []
        values: list[Any] = []
        if status is not None:
            updates.append("status = ?")
            values.append(status)
        if started_at is not None:
            updates.append("started_at = ?")
            values.append(started_at)
        if finished_at is not None:
            updates.append("finished_at = ?")
            values.append(finished_at)
        if progress_message is not None:
            updates.append("progress_message = ?")
            values.append(progress_message)
        if result is not None:
            updates.append("result_json = ?")
            values.append(json.dumps(result, ensure_ascii=True))
        if error_text is not None:
            updates.append("error_text = ?")
            values.append(error_text)
        if not updates:
            return self.get_scan_job(job_id)
        values.append(job_id)
        with self._connect() as connection:
            connection.execute(f"UPDATE scan_jobs SET {', '.join(updates)} WHERE id = ?", tuple(values))
            connection.commit()
            row = self._fetchone_dict(connection, "SELECT * FROM scan_jobs WHERE id = ?", (job_id,))
        return self._normalize_scan_job(row)

    def get_scan_job(self, job_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = self._fetchone_dict(connection, "SELECT * FROM scan_jobs WHERE id = ?", (job_id,))
        return self._normalize_scan_job(row)

    def get_active_scan_job(self, *, job_type: str = "code_atlas") -> dict[str, Any] | None:
        with self._connect() as connection:
            row = self._fetchone_dict(
                connection,
                """
                SELECT * FROM scan_jobs
                WHERE project_path = ? AND job_type = ? AND status IN ('queued', 'running')
                ORDER BY requested_at DESC
                LIMIT 1
                """,
                (self.project_root, job_type),
            )
        return self._normalize_scan_job(row)

    def list_scan_jobs(self, status: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
        with self._connect() as connection:
            if status:
                rows = self._fetchall_dicts(
                    connection,
                    "SELECT * FROM scan_jobs WHERE project_path = ? AND status = ? ORDER BY requested_at DESC LIMIT ?",
                    (self.project_root, status, limit),
                )
            else:
                rows = self._fetchall_dicts(
                    connection,
                    "SELECT * FROM scan_jobs WHERE project_path = ? ORDER BY requested_at DESC LIMIT ?",
                    (self.project_root, limit),
                )
        return [self._normalize_scan_job(row) for row in rows]

    def search_notes(self, query: str, limit: int = 10) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        normalized = query.lower()
        vault_root = self.project_config.vault_path
        for note_path in sorted(vault_root.rglob("*.md")):
            try:
                content = note_path.read_text(encoding="utf-8")
            except OSError:
                continue
            if normalized not in content.lower() and normalized not in note_path.name.lower():
                continue
            line_number = 1
            excerpt = ""
            for index, line in enumerate(content.splitlines(), start=1):
                if normalized in line.lower():
                    line_number = index
                    excerpt = line.strip()
                    break
            results.append(
                {
                    "path": str(note_path.relative_to(vault_root)).replace("\\", "/"),
                    "line": line_number,
                    "excerpt": excerpt,
                }
            )
            if len(results) >= limit:
                break
        return results

    def read_note(self, path: str) -> dict[str, Any]:
        vault_root = self.project_config.vault_path.resolve()
        note_path = (vault_root / path).resolve()
        if vault_root not in note_path.parents and note_path != vault_root:
            raise ValueError("Requested note is outside the Obsidian vault.")
        if not note_path.exists():
            raise FileNotFoundError(path)
        return {
            "path": str(note_path.relative_to(vault_root)).replace("\\", "/"),
            "content": note_path.read_text(encoding="utf-8"),
        }

    # =========================================================================
    # Phase 1: Task Templates
    # =========================================================================

    def get_task_templates(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = self._fetchall_dicts(connection, "SELECT * FROM task_templates ORDER BY name ASC")
        for row in rows:
            row["tags"] = parse_json(row.get("tags"), [])
        return rows

    def get_task_template(self, name: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = self._fetchone_dict(connection, "SELECT * FROM task_templates WHERE name = ?", (name,))
        if row:
            row["tags"] = parse_json(row.get("tags"), [])
        return row

    def create_task_template(
        self,
        name: str,
        title_template: str,
        description_template: str,
        priority: str = "medium",
        tags: list[str] | None = None,
    ) -> dict[str, Any]:
        now = utc_now()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO task_templates
                (name, title_template, description_template, priority, tags, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (name, title_template, description_template, priority, json.dumps(tags or []), now),
            )
            connection.commit()
        return self.get_task_template(name) or {}

    def delete_task_template(self, name: str) -> bool:
        with self._connect() as connection:
            cursor = connection.execute("DELETE FROM task_templates WHERE name = ?", (name,))
            connection.commit()
        return cursor.rowcount > 0

    def create_task_from_template(
        self,
        template_name: str,
        variables: dict[str, str] | None = None,
        actor: str = "unknown",
        session_id: str | None = None,
    ) -> dict[str, Any] | None:
        template = self.get_task_template(template_name)
        if not template:
            raise ValueError(f"Template '{template_name}' not found. Available: {[t['name'] for t in self.get_task_templates()]}")

        variables = variables or {}

        # Replace template variables
        def apply_template(text: str) -> str:
            result = text
            for key, value in variables.items():
                result = result.replace(f"{{{key}}}", value)
            # Fail if any placeholder remains
            import re

            remaining = re.findall(r"\{(\w+)\}", result)
            if remaining:
                raise ValueError(f"Missing template variables: {remaining}. Provide values for: {list(variables.keys())}")
            return result

        title = apply_template(template["title_template"])
        description = apply_template(template["description_template"])

        return self.create_task(
            title=title,
            description=description,
            priority=template["priority"],
            tags=template["tags"],
            actor=actor,
            session_id=session_id,
        )

    # =========================================================================
    # Phase 1: Quick Log
    # =========================================================================

    def quick_log(self, message: str, files: list[str] | None = None, actor: str = "quick-log", session_id: str | None = None) -> dict[str, Any]:
        current_task = self.get_current_task()
        task_id = current_task["id"] if current_task else None
        return self.log_work(message=message, task_id=task_id, actor=actor, files=files, session_id=session_id)

    # =========================================================================
    # Phase 1: Audit Log
    # =========================================================================

    def get_audit_log(
        self,
        actor: str | None = None,
        task_id: str | None = None,
        action_type: str | None = None,
        from_date: str | None = None,
        to_date: str | None = None,
        limit: int = 100,
        after_id: int | None = None,
        include_ai_only: bool = False,
    ) -> dict[str, Any]:
        conditions: list[str] = []
        params: list[Any] = []

        if actor:
            conditions.append("actor = ?")
            params.append(actor)
        if task_id:
            conditions.append("task_id = ?")
            params.append(task_id)
        if action_type:
            conditions.append("action = ?")
            params.append(action_type)
        if from_date:
            conditions.append("created_at >= ?")
            params.append(from_date)
        if to_date:
            conditions.append("created_at <= ?")
            params.append(to_date)
        if include_ai_only:
            conditions.append("actor NOT IN ('ctx', 'manual', 'human')")
        if after_id is not None:
            conditions.append("id < ?")
            params.append(after_id)

        where_clause = " AND ".join(conditions) if conditions else "1=1"
        query = f"SELECT * FROM agent_activity WHERE {where_clause} ORDER BY created_at DESC, id DESC LIMIT ?"
        params.append(limit)

        with self._connect() as connection:
            rows = self._fetchall_dicts(connection, query, tuple(params))

        # Summarize by actor and action type
        by_actor: dict[str, int] = {}
        by_action: dict[str, int] = {}
        for row in rows:
            a = row["actor"]
            ac = row["action"]
            by_actor[a] = by_actor.get(a, 0) + 1
            by_action[ac] = by_action.get(ac, 0) + 1

        events = []
        for seq, row in enumerate(rows, start=1):
            payload = parse_json(row.get("payload"), {})
            summary = ""
            if row["action"] == "log_work":
                summary = payload.get("message", "") or payload.get("summary", "")
            elif row["action"] == "create_task":
                summary = f"Created task: {payload.get('title', '')}"
            elif row["action"] == "update_task":
                fields = payload.get("fields", [])
                summary = f"Updated task fields: {', '.join(fields) if fields else 'unknown'}"
            elif row["action"] == "session_open":
                summary = f"Session opened by {payload.get('client_name', '')}"
            elif row["action"] == "session_close":
                summary = "Session closed"
            elif row["action"] == "log_decision":
                summary = f"Decision: {payload.get('title', '')}"
            elif row["action"] == "log_blocker":
                summary = f"Blocker: {payload.get('title', '')}"
            elif row["action"] == "resolve_blocker":
                summary = f"Resolved blocker #{payload.get('blocker_id', '')}"
            elif row["action"] == "create_handoff":
                summary = f"Handoff to {payload.get('to_actor', '')}"
            events.append(
                {
                    "seq": seq,
                    "id": row["id"],
                    "actor": row["actor"],
                    "action": row["action"],
                    "task_id": row["task_id"],
                    "time": row["created_at"],
                    "summary": summary,
                    "payload": payload,
                }
            )

        return {
            "total_events": len(events),
            "events": events,
            "by_actor": dict(sorted(by_actor.items(), key=lambda x: -x[1])),
            "by_action": dict(sorted(by_action.items(), key=lambda x: -x[1])),
            "has_more": len(events) == limit,
            "next_cursor": events[-1]["id"] if events else None,
        }

    # =========================================================================
    # Phase 2: Reset Project
    # =========================================================================

    VALID_RESET_SCOPES = {"tasks", "blockers", "sessions", "work_logs", "decisions", "handoffs", "full"}

    def reset_project(self, scope: str, actor: str = "unknown") -> dict[str, Any]:
        """Wipe project data by scope. Always creates audit trail before wiping."""
        if scope not in self.VALID_RESET_SCOPES:
            raise ValueError(f"Invalid scope '{scope}'. Valid: {sorted(self.VALID_RESET_SCOPES)}")

        now = utc_now()
        counts: dict[str, int] = {}

        with self._connect() as connection:
            if scope in {"tasks", "full"}:
                # Count before delete
                counts["tasks"] = connection.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
                counts["current_task"] = connection.execute("SELECT COUNT(*) FROM project_state WHERE key='current_task_id'").fetchone()[0]
                if counts["tasks"]:
                    connection.execute("DELETE FROM tasks")
                    connection.execute("UPDATE project_state SET value='', updated_at=? WHERE key='current_task_id'", (now,))

            if scope in {"blockers", "full"}:
                counts["blockers"] = connection.execute("SELECT COUNT(*) FROM blockers").fetchone()[0]
                if counts["blockers"]:
                    connection.execute("DELETE FROM blockers")

            if scope in {"sessions", "full"}:
                counts["sessions"] = connection.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
                counts["session_events"] = connection.execute("SELECT COUNT(*) FROM session_events").fetchone()[0]
                if counts["sessions"]:
                    connection.execute("DELETE FROM session_events")
                    connection.execute("DELETE FROM sessions")

            if scope in {"work_logs", "full"}:
                counts["work_logs"] = connection.execute("SELECT COUNT(*) FROM work_logs").fetchone()[0]
                if counts["work_logs"]:
                    connection.execute("DELETE FROM work_logs")

            if scope in {"decisions", "full"}:
                counts["decisions"] = connection.execute("SELECT COUNT(*) FROM decisions").fetchone()[0]
                if counts["decisions"]:
                    connection.execute("DELETE FROM decisions")

            if scope in {"handoffs", "full"}:
                counts["handoffs"] = connection.execute("SELECT COUNT(*) FROM handoffs").fetchone()[0]
                if counts["handoffs"]:
                    connection.execute("DELETE FROM handoffs")

            if scope == "full":
                # Keep brief sections and project state key only
                counts["brief_sections"] = 4  # Always 4 default sections
                connection.execute("DELETE FROM agent_activity")
                connection.execute("DELETE FROM session_summaries")
                connection.execute("DELETE FROM daily_entries")

            connection.commit()

        total_deleted = sum(v for k, v in counts.items() if k != "current_task" and k != "brief_sections")

        # Always record decision BEFORE wiping (survives the wipe)
        self.log_decision(
            title=f"Project reset: {scope}",
            decision=f"Reset scope '{scope}' — deleted {total_deleted} total records",
            rationale=f"Manual reset initiated by {actor}",
            impact=f"Counts: {counts}",
            actor=actor,
        )

        return {
            "scope": scope,
            "counts": counts,
            "total_deleted": total_deleted,
            "message": f"Project reset complete. Deleted {total_deleted} records across scopes: {scope}",
        }

    # =========================================================================
    # Phase 2: Bulk Task Operations
    # =========================================================================

    def bulk_task_ops(self, operations: list[dict[str, Any]], actor: str = "unknown") -> dict[str, Any]:
        """Execute multiple task operations atomically. All succeed or all fail."""
        if not operations:
            return {"results": [], "total": 0, "succeeded": 0, "failed": 0}

        VALID_ACTIONS = {"create", "update", "close", "delete", "set_current"}
        results: list[dict[str, Any]] = []

        # Pre-validate all operations
        for i, op in enumerate(operations):
            action = op.get("action")
            if action not in VALID_ACTIONS:
                raise ValueError(f"Operation {i}: invalid action '{action}'. Valid: {VALID_ACTIONS}")
            if action in {"update", "close", "delete", "set_current"} and not op.get("task_id"):
                raise ValueError(f"Operation {i}: action '{action}' requires task_id")

        with self._connect() as connection:
            for i, op in enumerate(operations):
                try:
                    action = op["action"]
                    task_id = op.get("task_id")

                    if action == "create":
                        result = self.create_task(
                            title=op["title"],
                            description=op.get("description", ""),
                            priority=op.get("priority", "medium"),
                            owner=op.get("owner"),
                            relevant_files=op.get("relevant_files"),
                            tags=op.get("tags"),
                            actor=actor,
                        )
                        results.append({"seq": i + 1, "action": action, "status": "success", "result": result})

                    elif action == "update":
                        # Extract allowed fields
                        fields = {k: v for k, v in op.items() if k in {"title", "description", "status", "priority", "owner", "relevant_files", "tags"} and v is not None}
                        result = self.update_task(task_id=task_id, actor=actor, **fields)
                        results.append({"seq": i + 1, "action": action, "task_id": task_id, "status": "success", "result": result})

                    elif action == "close":
                        result = self.update_task(task_id=task_id, actor=actor, status="done")
                        results.append({"seq": i + 1, "action": action, "task_id": task_id, "status": "success", "result": result})

                    elif action == "delete":
                        cursor = connection.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
                        connection.commit()
                        deleted = cursor.rowcount > 0
                        results.append({"seq": i + 1, "action": action, "task_id": task_id, "status": "success" if deleted else "not_found", "deleted": deleted})

                    elif action == "set_current":
                        result = self.set_current_task(task_id=task_id, actor=actor)
                        results.append({"seq": i + 1, "action": action, "task_id": task_id, "status": "success", "result": result})

                except Exception as e:
                    # Atomic: rollback all on any failure
                    connection.rollback()
                    results.append({"seq": i + 1, "action": op.get("action"), "task_id": op.get("task_id"), "status": "error", "error": str(e)})
                    return {
                        "results": results,
                        "total": len(operations),
                        "succeeded": sum(1 for r in results if r["status"] == "success"),
                        "failed": len(operations),
                        "message": "Bulk operation failed — atomic rollback performed. No changes were made.",
                    }

        return {
            "results": results,
            "total": len(operations),
            "succeeded": sum(1 for r in results if r["status"] == "success"),
            "failed": sum(1 for r in results if r["status"] != "success"),
            "message": f"Bulk ops complete. {sum(1 for r in results if r['status'] == 'success')}/{len(operations)} succeeded.",
        }

    # =========================================================================
    # Phase 2: Project Export
    # =========================================================================

    def export_project(self, format: str = "json") -> dict[str, Any]:
        """Export full project state as JSON and/or Markdown bundle."""
        import gzip

        if format not in {"json", "markdown", "both"}:
            raise ValueError(f"Invalid format '{format}'. Valid: json, markdown, both")

        now = utc_now()
        timestamp = now.replace(":", "-").replace("Z", "")
        export_dir = self.project_config.export_dir / f"obsmcp-export-{timestamp}"
        export_dir.mkdir(parents=True, exist_ok=True)

        # Gather all state
        export_data = {
            "project_name": self.project_config.project_name,
            "project_slug": self.project_config.project_slug,
            "project_path": self.project_root,
            "exported_at": now,
            "brief": self.get_project_brief(),
            "current_task": self.get_current_task(),
            "active_tasks": self.get_active_tasks(limit=999),
            "blockers": self.get_blockers(open_only=False, limit=999),
            "decisions": self.get_decisions(limit=999),
            "handoffs": [],
            "work_logs": self.get_recent_work(limit=999),
            "sessions": self.get_active_sessions(limit=999),
            "audit": self.get_audit_log(limit=999),
        }

        # Get handoffs
        with self._connect() as conn:
            export_data["handoffs"] = self._fetchall_dicts(conn, "SELECT * FROM handoffs ORDER BY created_at DESC LIMIT 999")

        exported_files: list[str] = []

        # JSON export
        if format in {"json", "both"}:
            json_path = export_dir / "obsmcp-export.json"
            with gzip.open(json_path.with_suffix(".json.gz"), "wt", encoding="utf-8") as f:
                import json as _json

                f.write(_json.dumps(export_data, indent=2, ensure_ascii=False))
            exported_files.append(str(json_path.with_suffix(".json.gz")))

        # Markdown export
        if format in {"markdown", "both"}:
            # Project Brief
            brief_path = export_dir / "00_Project_Brief.md"
            brief_lines = ["# Project Brief\n"]
            for section, content in export_data["brief"].items():
                brief_lines.append(f"## {section}\n{content}\n")
            brief_path.write_text("\n".join(brief_lines), encoding="utf-8")
            exported_files.append(str(brief_path))

            # Tasks
            tasks_path = export_dir / "01_Tasks.md"
            task_lines = ["# Tasks\n"]
            for task in export_data["active_tasks"]:
                task_lines.append(f"## {task['id']}: {task['title']}\n- Status: {task['status']}\n- Priority: {task['priority']}\n- Created: {task['created_at']}\n{task['description']}\n")
            tasks_path.write_text("\n".join(task_lines), encoding="utf-8")
            exported_files.append(str(tasks_path))

            # Decisions
            decisions_path = export_dir / "02_Decisions.md"
            decision_lines = ["# Decisions\n"]
            for dec in export_data["decisions"]:
                decision_lines.append(f"## {dec['title']}\n- {dec['created_at']}\n**Decision:** {dec['decision']}\n**Rationale:** {dec['rationale']}\n**Impact:** {dec['impact']}\n")
            decisions_path.write_text("\n".join(decision_lines), encoding="utf-8")
            exported_files.append(str(decisions_path))

            # Work Logs
            logs_path = export_dir / "03_Work_Logs.md"
            log_lines = ["# Work Logs\n"]
            for log in export_data["work_logs"]:
                log_lines.append(f"- [{log['created_at']}] [{log['actor']}] {log['message']}\n")
            logs_path.write_text("\n".join(log_lines), encoding="utf-8")
            exported_files.append(str(logs_path))

            # Handoffs
            handoffs_path = export_dir / "04_Handoffs.md"
            handoff_lines = ["# Handoffs\n"]
            for h in export_data["handoffs"]:
                handoff_lines.append(f"## {h['from_actor']} → {h['to_actor']}\n- {h['created_at']}\n**Summary:** {h['summary']}\n**Next Steps:** {h['next_steps']}\n")
            handoffs_path.write_text("\n".join(handoff_lines), encoding="utf-8")
            exported_files.append(str(handoffs_path))

            # Manifest
            manifest_path = export_dir / "MANIFEST.md"
            manifest_lines = [
                f"# obsmcp Export — {timestamp}\n",
                f"**Exported:** {now}\n",
                f"**Project:** {self.project_config.project_name}\n",
                f"**Project Slug:** {self.project_config.project_slug}\n",
                f"**Repo Path:** {self.project_root}\n",
                "## Files\n",
            ]
            for fpath in sorted(exported_files):
                manifest_lines.append(f"- `{Path(fpath).name}`")
            manifest_path.write_text("\n".join(manifest_lines), encoding="utf-8")
            exported_files.append(str(manifest_path))

        # Record the decision
        self.log_decision(
            title=f"Project export: {format}",
            decision=f"Exported project state as {format} to {export_dir.name}",
            rationale=f"Manual export by user",
            impact=f"{len(exported_files)} files created",
            actor="export",
        )

        return {
            "format": format,
            "export_dir": str(export_dir),
            "files": exported_files,
            "file_count": len(exported_files),
            "exported_at": now,
            "message": f"Export complete. {len(exported_files)} files written to {export_dir}",
        }

    # =========================================================================
    # Phase 3: Work Log Expiry
    # =========================================================================

    def get_log_expiry_days(self) -> int:
        with self._connect() as conn:
            row = self._fetchone_dict(conn, "SELECT value FROM project_state WHERE key='log_expiry_days'")
        return int(row["value"]) if row and row["value"] else 0

    def configure_log_expiry(self, days: int, actor: str = "unknown") -> dict[str, Any]:
        now = utc_now()
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO project_state (key, value, updated_at) VALUES ('log_expiry_days', ?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                (str(days), now),
            )
            conn.commit()
        return {"log_expiry_days": days, "message": f"Log expiry set to {days} days. 0 = disabled."}

    def expire_old_logs(self, actor: str = "unknown") -> dict[str, Any]:
        days = self.get_log_expiry_days()
        if days <= 0:
            return {"deleted": 0, "message": "Log expiry is disabled (days=0). No logs purged."}

        from datetime import timedelta

        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
        with self._connect() as conn:
            # Never delete logs from open sessions
            open_session_ids = [r["id"] for r in self._fetchall_dicts(conn, "SELECT id FROM sessions WHERE status='open'")]
            if open_session_ids:
                placeholders = ",".join("?" * len(open_session_ids))
                cursor = conn.execute(
                    f"DELETE FROM work_logs WHERE created_at < ? AND task_id NOT IN (SELECT task_id FROM sessions WHERE id IN ({placeholders})) AND task_id IS NOT NULL",
                    (cutoff,) + tuple(open_session_ids),
                )
            else:
                cursor = conn.execute("DELETE FROM work_logs WHERE created_at < ?", (cutoff,))
            deleted = cursor.rowcount
            conn.commit()

        self.log_decision(
            title=f"Work log expiry: {days}-day retention",
            decision=f"Purged {deleted} work logs older than {days} days",
            rationale="Automatic log expiry cleanup",
            impact=f"{deleted} old records removed",
            actor=actor,
        )

        return {"deleted": deleted, "cutoff_date": cutoff, "retention_days": days, "message": f"Purged {deleted} old work logs."}

    def get_log_stats(self) -> dict[str, Any]:
        from datetime import timedelta

        now_dt = datetime.now(timezone.utc)
        week_ago = (now_dt - timedelta(days=7)).isoformat().replace("+00:00", "Z")
        month_ago = (now_dt - timedelta(days=30)).isoformat().replace("+00:00", "Z")
        three_months_ago = (now_dt - timedelta(days=90)).isoformat().replace("+00:00", "Z")

        with self._connect() as conn:
            total = conn.execute("SELECT COUNT(*) FROM work_logs").fetchone()[0]
            today_count = conn.execute("SELECT COUNT(*) FROM work_logs WHERE created_at >= ?", (now_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),)).fetchone()[0]
            week_count = conn.execute("SELECT COUNT(*) FROM work_logs WHERE created_at >= ? AND created_at < ?", (week_ago, now_dt.strftime("%Y-%m-%dT%H:%M:%SZ"))).fetchone()[0]
            month_count = conn.execute("SELECT COUNT(*) FROM work_logs WHERE created_at >= ? AND created_at < ?", (month_ago, week_ago)).fetchone()[0]
            three_month_count = conn.execute("SELECT COUNT(*) FROM work_logs WHERE created_at >= ? AND created_at < ?", (three_months_ago, month_ago)).fetchone()[0]
            older_count = conn.execute("SELECT COUNT(*) FROM work_logs WHERE created_at < ?", (three_months_ago,)).fetchone()[0]

        return {
            "total_logs": total,
            "expiry_days": self.get_log_expiry_days(),
            "buckets": {
                "today": today_count,
                "this_week": week_count,
                "this_month": month_count,
                "last_3_months": three_month_count,
                "older": older_count,
            },
            "message": f"Total: {total} logs. Older than 90 days: {older_count}",
        }

    # =========================================================================
    # Phase 3: Session Replay
    # =========================================================================

    def session_replay(self, session_id: str | None = None) -> dict[str, Any]:
        # If no session_id, get the most recent
        if not session_id:
            with self._connect() as conn:
                row = self._fetchone_dict(conn, "SELECT id FROM sessions ORDER BY opened_at DESC LIMIT 1")
            if not row:
                return {"error": "No sessions found."}
            session_id = row["id"]

        with self._connect() as conn:
            session = self._fetchone_dict(conn, "SELECT * FROM sessions WHERE id = ?", (session_id,))
            if not session:
                return {"error": f"Session '{session_id}' not found."}

            events = self._fetchall_dicts(conn, "SELECT * FROM session_events WHERE session_id = ? ORDER BY created_at ASC", (session_id,))

        if not session:
            return {"error": f"Session '{session_id}' not found."}

        # Parse session timestamps
        opened = datetime.fromisoformat(session["opened_at"].replace("Z", "+00:00"))
        closed = datetime.fromisoformat(session["closed_at"].replace("Z", "+00:00")) if session["closed_at"] else datetime.now(timezone.utc)
        duration = int((closed - opened).total_seconds())

        # Build event timeline
        event_list = []
        events_by_type: dict[str, int] = {}
        warnings: list[str] = []
        prev_event_time = opened

        for i, evt in enumerate(events):
            evt_time = datetime.fromisoformat(evt["created_at"].replace("Z", "+00:00"))
            gap = int((evt_time - prev_event_time).total_seconds())

            if gap > 900:  # > 15 minutes
                warnings.append(f"Gap of {gap}s before {evt['event_type']} at {evt['created_at']}")

            payload = parse_json(evt.get("payload"), {})
            events_by_type[evt["event_type"]] = events_by_type.get(evt["event_type"], 0) + 1

            event_list.append({
                "seq": i + 1,
                "time": evt["created_at"],
                "actor": evt["actor"],
                "event_type": evt["event_type"],
                "payload": payload,
                "gap_seconds": gap,
            })
            prev_event_time = evt_time

        # Check for missed heartbeats
        heartbeat_interval = session["heartbeat_interval_seconds"]
        last_hb = datetime.fromisoformat(session["heartbeat_at"].replace("Z", "+00:00"))
        overdue = int((closed - last_hb).total_seconds())
        if session["status"] == "open" and overdue > heartbeat_interval:
            warnings.append(f"Heartbeat overdue by {overdue}s (interval: {heartbeat_interval}s)")
        if session["status"] == "closed" and not session.get("handoff_created"):
            warnings.append("Session closed without a recorded handoff.")

        # Render as Markdown
        md_lines = [
            f"# Session Replay: {session_id}",
            "",
            f"**Actor:** {session['actor']}  ",
            f"**Client:** {session['client_name']}  ",
            f"**Model:** {session['model_name']}  ",
            f"**Goal:** {session['session_goal']}",
            f"**Opened:** {session['opened_at']}  ",
            f"**Closed:** {session['closed_at'] or 'still open'}  ",
            f"**Duration:** {duration}s ({duration // 60}m {duration % 60}s)",
            "",
            "## Timeline",
            "",
        ]
        for evt in event_list:
            payload_str = ""
            payload = evt["payload"]
            if evt["event_type"] == "log_work":
                payload_str = f" — {payload.get('message', '')[:80]}"
            elif evt["event_type"] == "create_task":
                payload_str = f" — {payload.get('title', '')}"
            elif evt["event_type"] == "session_open":
                payload_str = f" — {payload.get('client_name', '')} / {payload.get('model_name', '')}"
            gap_note = f" (gap: {evt['gap_seconds']}s)" if evt["gap_seconds"] > 60 else ""
            md_lines.append(f"{evt['seq']}. **{evt['time']}** [{evt['actor']}] {evt['event_type']}{gap_note}{payload_str}")

        if warnings:
            md_lines.extend(["", "## Warnings", ""])
            for w in warnings:
                md_lines.append(f"- ⚠️ {w}")

        md_lines.extend(["", "## Statistics", ""])
        md_lines.append(f"- Total events: {len(event_list)}")
        md_lines.append(f"- Write count: {session['write_count']}")
        md_lines.append(f"- Duration: {duration}s")
        md_lines.append(f"- Events by type: {events_by_type}")

        return {
            "session_id": session_id,
            "session": session,
            "events": event_list,
            "statistics": {
                "total_events": len(event_list),
                "events_by_type": events_by_type,
                "total_duration_seconds": duration,
                "write_count": session["write_count"],
                "warnings": warnings,
            },
            "timeline_markdown": "\n".join(md_lines),
        }

    # =========================================================================
    # Phase 3: Task Dependencies
    # =========================================================================

    def add_task_dependency(self, task_id: str, blocked_by: list[str] | None = None, blocks: list[str] | None = None) -> dict[str, Any]:
        now = utc_now()
        blocked_by = blocked_by or []
        blocks = blocks or []

        # Validate task exists
        task = self.get_task(task_id)
        if not task:
            raise ValueError(f"Task '{task_id}' not found.")

        for dep_id in blocked_by + blocks:
            dep = self.get_task(dep_id)
            if not dep:
                raise ValueError(f"Dependency task '{dep_id}' not found.")

        # Check for cycles
        if self._would_create_cycle(task_id, blocked_by, blocks):
            raise ValueError(f"Adding these dependencies would create a circular dependency for task '{task_id}'.")

        with self._connect() as conn:
            # Get existing dependencies
            existing = self._fetchone_dict(conn, "SELECT * FROM task_dependencies WHERE task_id = ?", (task_id,))
            existing_blocked_by = parse_json(existing["blocked_by"], []) if existing else []
            existing_blocks = parse_json(existing["blocks"], []) if existing else []

            new_blocked_by = list(set(existing_blocked_by + blocked_by))
            new_blocks = list(set(existing_blocks + blocks))

            conn.execute(
                """
                INSERT INTO task_dependencies (task_id, blocked_by, blocks, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(task_id) DO UPDATE SET blocked_by=excluded.blocked_by, blocks=excluded.blocks, updated_at=excluded.updated_at
                """,
                (task_id, json.dumps(new_blocked_by), json.dumps(new_blocks), now),
            )
            conn.commit()

        return self.get_task_dependency(task_id) or {}

    def remove_task_dependency(self, task_id: str, blocked_by: list[str] | None = None, blocks: list[str] | None = None) -> dict[str, Any]:
        now = utc_now()
        with self._connect() as conn:
            existing = self._fetchone_dict(conn, "SELECT * FROM task_dependencies WHERE task_id = ?", (task_id,))
            if not existing:
                return {"task_id": task_id, "blocked_by": [], "blocks": [], "message": "No dependencies found."}

            existing_blocked_by = parse_json(existing["blocked_by"], [])
            existing_blocks = parse_json(existing["blocks"], [])

            new_blocked_by = [x for x in existing_blocked_by if x not in (blocked_by or [])]
            new_blocks = [x for x in existing_blocks if x not in (blocks or [])]

            if not new_blocked_by and not new_blocks:
                conn.execute("DELETE FROM task_dependencies WHERE task_id = ?", (task_id,))
            else:
                conn.execute(
                    "UPDATE task_dependencies SET blocked_by=?, blocks=?, updated_at=? WHERE task_id=?",
                    (json.dumps(new_blocked_by), json.dumps(new_blocks), now, task_id),
                )
            conn.commit()

        return {"task_id": task_id, "blocked_by": new_blocked_by, "blocks": new_blocks}

    def get_task_dependency(self, task_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = self._fetchone_dict(conn, "SELECT * FROM task_dependencies WHERE task_id = ?", (task_id,))
        if not row:
            return None
        return {
            "task_id": row["task_id"],
            "blocked_by": parse_json(row["blocked_by"], []),
            "blocks": parse_json(row["blocks"], []),
            "updated_at": row["updated_at"],
        }

    def get_all_dependencies(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = self._fetchall_dicts(conn, "SELECT * FROM task_dependencies")
        result = []
        for row in rows:
            result.append({
                "task_id": row["task_id"],
                "blocked_by": parse_json(row["blocked_by"], []),
                "blocks": parse_json(row["blocks"], []),
                "updated_at": row["updated_at"],
            })
        return result

    def get_blocked_tasks(self) -> list[dict[str, Any]]:
        """Return tasks that are blocked by unresolved task IDs."""
        all_deps = self.get_all_dependencies()
        active_task_ids = {t["id"] for t in self.get_active_tasks(limit=999)}
        blocked_tasks = []

        for dep in all_deps:
            unresolved_blockers = [bid for bid in dep["blocked_by"] if bid in active_task_ids]
            if unresolved_blockers:
                task = self.get_task(dep["task_id"])
                if task:
                    task_copy = dict(task)
                    task_copy["unresolved_blockers"] = unresolved_blockers
                    blocked_tasks.append(task_copy)

        return blocked_tasks

    def validate_dependencies(self) -> dict[str, Any]:
        """Check for circular dependencies and broken references."""
        issues: list[str] = []
        all_deps = self.get_all_dependencies()
        dep_map: dict[str, set[str]] = {}

        for dep in all_deps:
            dep_map[dep["task_id"]] = set(dep["blocked_by"])

        # Check for broken references
        all_task_ids = {t["id"] for t in self.get_active_tasks(limit=9999)}
        for dep in all_deps:
            for bid in dep["blocked_by"]:
                if bid not in all_task_ids:
                    issues.append(f"Task '{dep['task_id']}' references non-existent blocker '{bid}'")

        # Check for cycles using DFS
        visited: set[str] = set()
        rec_stack: set[str] = set()

        def has_cycle(task_id: str, path: list[str]) -> bool:
            visited.add(task_id)
            rec_stack.add(task_id)
            path.append(task_id)

            for blocker in dep_map.get(task_id, []):
                if blocker not in visited:
                    if has_cycle(blocker, path[:]):
                        return True
                elif blocker in rec_stack:
                    issues.append(f"Circular dependency detected: {' -> '.join(path + [blocker])}")
                    return True

            rec_stack.remove(task_id)
            return False

        for task_id in dep_map:
            if task_id not in visited:
                has_cycle(task_id, [])

        return {
            "valid": len(issues) == 0,
            "issues": issues,
            "total_dependencies": len(all_deps),
            "message": f"Dependency validation: {'PASSED — no issues' if not issues else f'{len(issues)} issue(s) found'}",
        }

    def _would_create_cycle(self, task_id: str, blocked_by: list[str], blocks: list[str]) -> bool:
        """Check if adding blocked_by/blocks to task_id would create a cycle."""
        all_deps = self.get_all_dependencies()

        # Build graph: key = task, value = set of tasks it depends on (i.e., blocked by)
        # From blocked_by: X.blocked_by=[A,B] → X depends on A,B → A→X, B→X
        # From blocks: X.blocks=[P,Q] → P depends on X, Q depends on X → P→X, Q→X
        # Note: blocked_by is the canonical direction; blocks is the inverse relationship
        dep_map: dict[str, set[str]] = {}
        for dep in all_deps:
            tid = dep["task_id"]
            dep_map.setdefault(tid, set()).update(dep["blocked_by"])
            for blocked in dep["blocks"]:
                dep_map.setdefault(blocked, set()).add(tid)

        # Temporarily add the new edges
        # blocked_by: A.blocked_by=[B] → A depends on B → A→B
        dep_map.setdefault(task_id, set()).update(blocked_by)
        # blocks: A.blocks=[B] → B depends on A → B→A
        for blocked_task in blocks:
            dep_map.setdefault(blocked_task, set()).add(task_id)

        # DFS cycle detection
        def dfs(tid: str, path: set[str]) -> bool:
            if tid in path:
                return True
            path.add(tid)
            for dep_tid in dep_map.get(tid, []):
                if dfs(dep_tid, path):
                    return True
            path.discard(tid)
            return False

        return dfs(task_id, set())
