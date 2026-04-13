from __future__ import annotations

import sqlite3
from pathlib import Path


SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS project_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS project_brief_sections (
    section TEXT PRIMARY KEY,
    content TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    description TEXT NOT NULL,
    status TEXT NOT NULL,
    priority TEXT NOT NULL,
    owner TEXT,
    relevant_files TEXT NOT NULL,
    tags TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    started_at TEXT,
    completed_at TEXT
);

CREATE TABLE IF NOT EXISTS work_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT,
    actor TEXT NOT NULL,
    message TEXT NOT NULL,
    summary TEXT,
    files TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (task_id) REFERENCES tasks(id)
);

CREATE TABLE IF NOT EXISTS checkpoints (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL,
    checkpoint_id TEXT NOT NULL,
    title TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'completed',
    message TEXT NOT NULL DEFAULT '',
    files TEXT NOT NULL DEFAULT '[]',
    actor TEXT NOT NULL,
    session_id TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(task_id, checkpoint_id),
    FOREIGN KEY (task_id) REFERENCES tasks(id),
    FOREIGN KEY (session_id) REFERENCES sessions(id)
);

CREATE INDEX IF NOT EXISTS idx_checkpoints_task_created
ON checkpoints(task_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_checkpoints_created
ON checkpoints(created_at DESC);

CREATE TABLE IF NOT EXISTS decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    decision TEXT NOT NULL,
    rationale TEXT NOT NULL,
    impact TEXT NOT NULL,
    task_id TEXT,
    actor TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (task_id) REFERENCES tasks(id)
);

CREATE TABLE IF NOT EXISTS blockers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    description TEXT NOT NULL,
    task_id TEXT,
    actor TEXT NOT NULL,
    status TEXT NOT NULL,
    resolution_note TEXT,
    created_at TEXT NOT NULL,
    resolved_at TEXT,
    FOREIGN KEY (task_id) REFERENCES tasks(id)
);

CREATE TABLE IF NOT EXISTS handoffs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT,
    from_actor TEXT NOT NULL,
    to_actor TEXT NOT NULL,
    summary TEXT NOT NULL,
    next_steps TEXT NOT NULL,
    open_questions TEXT NOT NULL,
    note TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (task_id) REFERENCES tasks(id)
);

CREATE TABLE IF NOT EXISTS session_summaries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_label TEXT NOT NULL,
    summary TEXT NOT NULL,
    actor TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS daily_entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    note_date TEXT NOT NULL,
    actor TEXT NOT NULL,
    entry TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS agent_activity (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    actor TEXT NOT NULL,
    action TEXT NOT NULL,
    task_id TEXT,
    payload TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (task_id) REFERENCES tasks(id)
);

CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    actor TEXT NOT NULL,
    client_name TEXT NOT NULL,
    model_name TEXT NOT NULL,
    session_label TEXT NOT NULL DEFAULT '',
    workstream_key TEXT NOT NULL DEFAULT '',
    workstream_title TEXT NOT NULL DEFAULT '',
    project_path TEXT NOT NULL,
    initial_request TEXT NOT NULL,
    session_goal TEXT NOT NULL,
    task_id TEXT,
    status TEXT NOT NULL,
    opened_at TEXT NOT NULL,
    heartbeat_at TEXT NOT NULL,
    closed_at TEXT,
    require_heartbeat INTEGER NOT NULL,
    require_work_log INTEGER NOT NULL,
    heartbeat_interval_seconds INTEGER NOT NULL,
    work_log_interval_seconds INTEGER NOT NULL,
    min_work_logs INTEGER NOT NULL,
    write_count INTEGER NOT NULL,
    last_write_at TEXT,
    last_handoff_at TEXT,
    closure_summary TEXT,
    handoff_required INTEGER NOT NULL,
    handoff_created INTEGER NOT NULL,
    last_error TEXT,
    ide_name TEXT NOT NULL DEFAULT '',
    ide_version TEXT NOT NULL DEFAULT '',
    ide_platform TEXT NOT NULL DEFAULT '',
    os_name TEXT NOT NULL DEFAULT '',
    os_version TEXT NOT NULL DEFAULT '',
    FOREIGN KEY (task_id) REFERENCES tasks(id)
);

CREATE TABLE IF NOT EXISTS session_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    actor TEXT NOT NULL,
    event_type TEXT NOT NULL,
    payload TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (session_id) REFERENCES sessions(id)
);

CREATE TABLE IF NOT EXISTS task_templates (
    name TEXT PRIMARY KEY,
    title_template TEXT NOT NULL,
    description_template TEXT NOT NULL,
    priority TEXT NOT NULL DEFAULT 'medium',
    tags TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS task_dependencies (
    task_id TEXT PRIMARY KEY,
    blocked_by TEXT NOT NULL DEFAULT '[]',
    blocks TEXT NOT NULL DEFAULT '[]',
    updated_at TEXT NOT NULL,
    FOREIGN KEY (task_id) REFERENCES tasks(id)
);

CREATE TABLE IF NOT EXISTS semantic_file_fingerprints (
    file_path TEXT PRIMARY KEY,
    fingerprint TEXT NOT NULL,
    file_size INTEGER NOT NULL,
    modified_at TEXT NOT NULL,
    scanned_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS semantic_symbol_index (
    entity_key TEXT PRIMARY KEY,
    entity_type TEXT NOT NULL,
    name TEXT NOT NULL,
    file_path TEXT NOT NULL,
    symbol_path TEXT NOT NULL,
    signature TEXT NOT NULL,
    line_number INTEGER NOT NULL,
    feature_tags TEXT NOT NULL,
    source_files TEXT NOT NULL,
    source_fingerprint TEXT NOT NULL,
    summary_hint TEXT NOT NULL,
    metadata TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_semantic_symbol_index_type_name
ON semantic_symbol_index(entity_type, name);

CREATE INDEX IF NOT EXISTS idx_semantic_symbol_index_file
ON semantic_symbol_index(file_path);

CREATE TABLE IF NOT EXISTS semantic_descriptions (
    entity_key TEXT PRIMARY KEY,
    entity_type TEXT NOT NULL,
    name TEXT NOT NULL,
    file_path TEXT NOT NULL,
    symbol_path TEXT NOT NULL,
    signature TEXT NOT NULL,
    purpose TEXT NOT NULL,
    why_it_exists TEXT NOT NULL,
    how_it_is_used TEXT NOT NULL,
    inputs_outputs TEXT NOT NULL,
    side_effects TEXT NOT NULL,
    risks TEXT NOT NULL,
    related_files TEXT NOT NULL,
    related_decisions TEXT NOT NULL,
    related_tasks TEXT NOT NULL,
    related_symbols TEXT NOT NULL,
    source_fingerprint TEXT NOT NULL,
    generated_at TEXT NOT NULL,
    verified_at TEXT NOT NULL,
    stale INTEGER NOT NULL DEFAULT 0,
    metadata TEXT NOT NULL,
    llm_model TEXT,
    llm_latency_ms REAL,
    llm_input_tokens INTEGER,
    llm_output_tokens INTEGER,
    llm_generated INTEGER NOT NULL DEFAULT 0,
    language TEXT
);

CREATE INDEX IF NOT EXISTS idx_semantic_descriptions_type_name
ON semantic_descriptions(entity_type, name);

CREATE TABLE IF NOT EXISTS scan_jobs (
    id TEXT PRIMARY KEY,
    job_type TEXT NOT NULL,
    project_path TEXT NOT NULL,
    status TEXT NOT NULL,
    requested_by TEXT NOT NULL,
    force_refresh INTEGER NOT NULL DEFAULT 0,
    requested_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    progress_message TEXT NOT NULL DEFAULT '',
    result_json TEXT NOT NULL DEFAULT '{}',
    error_text TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_scan_jobs_project_status
ON scan_jobs(project_path, status, requested_at DESC);

CREATE TABLE IF NOT EXISTS context_artifacts (
    artifact_key TEXT PRIMARY KEY,
    artifact_type TEXT NOT NULL,
    scope_key TEXT NOT NULL,
    params_signature TEXT NOT NULL,
    state_version TEXT NOT NULL,
    content TEXT NOT NULL,
    metadata TEXT NOT NULL,
    generated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_context_artifacts_lookup
ON context_artifacts(artifact_type, scope_key, generated_at DESC);

CREATE TABLE IF NOT EXISTS token_usage_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type TEXT NOT NULL,
    operation TEXT NOT NULL,
    actor TEXT NOT NULL,
    session_id TEXT,
    task_id TEXT,
    model_name TEXT NOT NULL DEFAULT '',
    provider TEXT NOT NULL DEFAULT '',
    client_name TEXT NOT NULL DEFAULT '',
    raw_input_tokens INTEGER NOT NULL DEFAULT 0,
    raw_output_tokens INTEGER NOT NULL DEFAULT 0,
    estimated_input_tokens INTEGER NOT NULL DEFAULT 0,
    estimated_output_tokens INTEGER NOT NULL DEFAULT 0,
    compact_input_tokens INTEGER NOT NULL DEFAULT 0,
    compact_output_tokens INTEGER NOT NULL DEFAULT 0,
    saved_tokens INTEGER NOT NULL DEFAULT 0,
    cache_creation_input_tokens INTEGER NOT NULL DEFAULT 0,
    cache_read_input_tokens INTEGER NOT NULL DEFAULT 0,
    raw_chars INTEGER NOT NULL DEFAULT 0,
    compact_chars INTEGER NOT NULL DEFAULT 0,
    metadata TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_token_usage_events_created
ON token_usage_events(created_at DESC);

CREATE INDEX IF NOT EXISTS idx_token_usage_events_operation
ON token_usage_events(operation, created_at DESC);

CREATE TABLE IF NOT EXISTS command_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT,
    task_id TEXT,
    actor TEXT NOT NULL,
    command_text TEXT NOT NULL,
    cwd TEXT NOT NULL DEFAULT '',
    event_kind TEXT NOT NULL DEFAULT 'completed',
    status TEXT NOT NULL DEFAULT 'completed',
    risk_level TEXT NOT NULL DEFAULT 'normal',
    exit_code INTEGER NOT NULL DEFAULT 0,
    duration_ms INTEGER NOT NULL DEFAULT 0,
    summary TEXT NOT NULL DEFAULT '',
    stdout_summary TEXT NOT NULL DEFAULT '',
    stderr_summary TEXT NOT NULL DEFAULT '',
    output_profile TEXT NOT NULL DEFAULT '',
    raw_capture_id TEXT,
    raw_output_available INTEGER NOT NULL DEFAULT 0,
    files_changed TEXT NOT NULL DEFAULT '[]',
    metadata TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    FOREIGN KEY (session_id) REFERENCES sessions(id),
    FOREIGN KEY (task_id) REFERENCES tasks(id)
);

-- Phase 1: Cursor pagination indexes
CREATE INDEX IF NOT EXISTS idx_agent_activity_pagination
ON agent_activity(created_at DESC, id DESC);

CREATE INDEX IF NOT EXISTS idx_work_logs_pagination
ON work_logs(created_at DESC, id);

CREATE INDEX IF NOT EXISTS idx_decisions_pagination
ON decisions(created_at DESC, id);

CREATE INDEX IF NOT EXISTS idx_blockers_pagination
ON blockers(created_at DESC, id);

CREATE INDEX IF NOT EXISTS idx_command_events_pagination
ON command_events(created_at DESC, id DESC);

CREATE INDEX IF NOT EXISTS idx_sessions_active_pagination
ON sessions(status, heartbeat_at DESC);

CREATE INDEX IF NOT EXISTS idx_command_events_session
ON command_events(session_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_command_events_task
ON command_events(task_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_command_events_status
ON command_events(status, created_at DESC);

CREATE TABLE IF NOT EXISTS raw_output_captures (
    capture_id TEXT PRIMARY KEY,
    session_id TEXT,
    task_id TEXT,
    actor TEXT NOT NULL,
    command_text TEXT NOT NULL,
    profile TEXT NOT NULL,
    reason TEXT NOT NULL,
    exit_code INTEGER NOT NULL DEFAULT 0,
    output_path TEXT NOT NULL,
    preview TEXT NOT NULL DEFAULT '',
    raw_chars INTEGER NOT NULL DEFAULT 0,
    raw_tokens_est INTEGER NOT NULL DEFAULT 0,
    metadata TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_raw_output_captures_created
ON raw_output_captures(created_at DESC);

CREATE TABLE IF NOT EXISTS session_env_info (
    session_id TEXT PRIMARY KEY,
    ide_name TEXT NOT NULL DEFAULT '',
    ide_version TEXT NOT NULL DEFAULT '',
    ide_platform TEXT NOT NULL DEFAULT '',
    os_name TEXT NOT NULL DEFAULT '',
    os_version TEXT NOT NULL DEFAULT '',
    env_variables TEXT NOT NULL DEFAULT '{}',
    startup_context TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (session_id) REFERENCES sessions(id)
);

CREATE TABLE IF NOT EXISTS cross_tool_handoffs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    handoff_id INTEGER NOT NULL,
    target_tool TEXT NOT NULL,
    target_env TEXT NOT NULL,
    structured_payload TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (handoff_id) REFERENCES handoffs(id)
);

CREATE TABLE IF NOT EXISTS session_lineage (
    session_id TEXT PRIMARY KEY,
    parent_session_id TEXT,
    continuation_session_id TEXT,
    lineage_depth INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    FOREIGN KEY (session_id) REFERENCES sessions(id)
);
"""


class Database:
    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, check_same_thread=False)
        connection.row_factory = sqlite3.Row
        return connection

    def initialize(self) -> None:
        with self.connect() as connection:
            connection.executescript(SCHEMA)
            connection.commit()
