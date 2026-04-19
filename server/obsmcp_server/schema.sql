-- OBSMCP SQLite schema. All CREATE TABLE statements are idempotent.

CREATE TABLE IF NOT EXISTS projects (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    path TEXT NOT NULL,
    repo_url TEXT,
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    project_id TEXT,
    title TEXT NOT NULL,
    description TEXT,
    status TEXT DEFAULT 'open',
    priority TEXT DEFAULT 'medium',
    tags TEXT,
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    project_id TEXT,
    agent_id TEXT NOT NULL,
    started_at TEXT DEFAULT (datetime('now')),
    ended_at TEXT,
    duration_seconds INTEGER,
    context TEXT
);

CREATE TABLE IF NOT EXISTS blockers (
    id TEXT PRIMARY KEY,
    project_id TEXT,
    agent_id TEXT,
    description TEXT NOT NULL,
    severity TEXT DEFAULT 'medium',
    status TEXT DEFAULT 'active',
    resolved_at TEXT,
    resolution TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS decisions (
    id TEXT PRIMARY KEY,
    project_id TEXT,
    agent_id TEXT,
    decision TEXT NOT NULL,
    context TEXT,
    outcome TEXT,
    tags TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS work_logs (
    id TEXT PRIMARY KEY,
    project_id TEXT,
    session_id TEXT,
    agent_id TEXT,
    description TEXT NOT NULL,
    hours REAL,
    tags TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS code_atlas_scans (
    id TEXT PRIMARY KEY,
    project_id TEXT,
    agent_id TEXT,
    status TEXT DEFAULT 'pending',
    total_files INTEGER DEFAULT 0,
    scanned_files INTEGER DEFAULT 0,
    started_at TEXT DEFAULT (datetime('now')),
    completed_at TEXT,
    error_message TEXT
);

CREATE TABLE IF NOT EXISTS code_atlas_files (
    id TEXT PRIMARY KEY,
    scan_id TEXT,
    project_id TEXT,
    file_path TEXT NOT NULL,
    language TEXT,
    functions_count INTEGER DEFAULT 0,
    imports TEXT,
    exports TEXT,
    semantic_description TEXT,
    tokens_used INTEGER DEFAULT 0,
    scanned_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS knowledge_nodes (
    id TEXT PRIMARY KEY,
    project_id TEXT,
    agent_id TEXT,
    node_type TEXT NOT NULL,
    name TEXT NOT NULL,
    description TEXT,
    metadata TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS knowledge_edges (
    id TEXT PRIMARY KEY,
    project_id TEXT,
    from_node_id TEXT NOT NULL,
    to_node_id TEXT NOT NULL,
    edge_type TEXT NOT NULL,
    weight REAL DEFAULT 1.0,
    metadata TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS performance_logs (
    id TEXT PRIMARY KEY,
    project_id TEXT,
    agent_id TEXT,
    session_id TEXT,
    metric_name TEXT NOT NULL,
    metric_value REAL,
    unit TEXT,
    tags TEXT,
    logged_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS agent_configs (
    agent_id TEXT PRIMARY KEY,
    project_id TEXT,
    display_name TEXT,
    machine_name TEXT,
    os_type TEXT,
    last_seen_at TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE INDEX IF NOT EXISTS idx_tasks_project ON tasks(project_id);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_sessions_project ON sessions(project_id);
CREATE INDEX IF NOT EXISTS idx_blockers_project ON blockers(project_id);
CREATE INDEX IF NOT EXISTS idx_decisions_project ON decisions(project_id);
CREATE INDEX IF NOT EXISTS idx_work_logs_project ON work_logs(project_id);
CREATE INDEX IF NOT EXISTS idx_code_atlas_scans_project ON code_atlas_scans(project_id);
CREATE INDEX IF NOT EXISTS idx_code_atlas_files_scan ON code_atlas_files(scan_id);
CREATE INDEX IF NOT EXISTS idx_nodes_project ON knowledge_nodes(project_id);
CREATE INDEX IF NOT EXISTS idx_edges_from ON knowledge_edges(from_node_id);
CREATE INDEX IF NOT EXISTS idx_edges_to ON knowledge_edges(to_node_id);
CREATE INDEX IF NOT EXISTS idx_perf_logs_project ON performance_logs(project_id);
CREATE INDEX IF NOT EXISTS idx_perf_logs_logged_at ON performance_logs(logged_at);
