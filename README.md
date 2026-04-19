# OBSMCP — Observable Machine Code Protocol

![Python](https://img.shields.io/badge/python-3.12%2B-blue)
![License](https://img.shields.io/badge/license-MIT-green)

OBSMCP is a three-tier observability system for autonomous coding agents:

1. **Local MCP tool** — runs on the developer's machine, watches the project, streams tasks/sessions/blockers/decisions/work logs/metrics/knowledge-graph edges into a local SQLite database and optionally a remote backend.
2. **FastAPI backend** — raw-SQL SQLite store with full CRUD for every entity, Server-Sent Events bus for real-time mutations, WebSocket mirror, Bearer-token auth.
3. **React dashboard** — TanStack-Query-driven SPA with 10 pages (Dashboard, Tasks, Sessions, Blockers, Decisions, Work Logs, Code Atlas, Knowledge Graph, Performance Logs, Settings), live-invalidated from the SSE event bus.

```
┌────────────────────────┐      local SQLite (always)
│  Local MCP Tool (you)  │──┬────────────────┐
│  • monitors  • scanners│  │   ↕ cloud sync (optional)
└────────┬───────────────┘  │                │
         │ stdio            │                ▼
         ▼                  │      ┌──────────────────┐
  Claude / Cursor / CLI     │      │  FastAPI backend │ ←→ React SPA
                            │      │  SSE / WS bus    │
                            └─────►│  SQLite store    │
                                   └──────────────────┘
```

## Operating modes

- **Standalone** (default) — everything lives in `~/.obsmcp/data/obsmcp.db`. The dashboard is served at <http://localhost:8000> by the bundled FastAPI server.
- **Cloud sync** — same local DB + every write is mirrored to a remote backend. The agent stays fully functional offline; writes flush to the server automatically when connectivity returns.

## Quickstart

### 1. Install the Python tool

```bash
python -m pip install -e ".[dev]"      # clones & editable install
# Optional: LLM-powered semantic descriptions
python -m pip install -e ".[llm]"
```

### 2. First-run setup

Windows:

```
start.bat
```

macOS / Linux:

```
./start.sh
```

You'll be prompted for:

- **Project path** (required) — path to the codebase you want to observe.
- **Backend URL** (optional) — leave blank to stay standalone.
- **API token** (optional) — required if the backend has `OBSMCP_API_TOKEN` set.

Config lands at `~/.obsmcp/config.json` (`%USERPROFILE%\.obsmcp\config.json` on Windows).

### 3. Run

`start.bat` / `start.sh` launches `python -m obsmcp`, which spins up:

- Session monitor + heartbeat
- File watcher (via `watchfiles`, optional)
- Git commit/branch monitor
- Performance monitor (CPU / memory / disk via `psutil`)
- Code Atlas scanner (regex-based multi-language metadata)
- Knowledge graph node extractor + edge builder
- (Standalone only) Local FastAPI dashboard at <http://localhost:8000>

### 4. Plug into an MCP client

Add a stdio server entry to Claude Desktop / Cursor / Claude Code:

```jsonc
{
  "mcpServers": {
    "obsmcp": {
      "command": "python",
      "args": ["-m", "obsmcp", "--mcp-stdio"]
    }
  }
}
```

Tool names exposed: `get_tasks`, `create_task`, `update_task`, `delete_task`, `log_blocker`, `resolve_blocker`, `log_decision`, `log_work`, `start_session`, `end_session`, `scan_codebase`, `get_scan_status`, `add_node`, `add_edge`, `query_graph`, `get_performance_summary`, `sync_state`.

## Cloud deployment

```bash
# Build everything (frontend → /server/obsmcp_server/frontend_dist, backend image)
docker compose up --build
```

Environment variables:

| Variable            | Default                      | Notes                                          |
|---------------------|------------------------------|------------------------------------------------|
| `OBSMCP_API_TOKEN`  | (unset → no auth)            | Required for cloud mode                        |
| `OBSMCP_DB_PATH`    | `~/.obsmcp/data/obsmcp.db`   | In Docker: `/data/obsmcp.db` (volume-mounted)  |
| `OBSMCP_HOST`       | `0.0.0.0`                    |                                                |
| `OBSMCP_PORT`       | `8000`                       |                                                |
| `ANTHROPIC_API_KEY` | (unset)                      | Enables LLM semantic descriptions (opt-in)     |

## API surface

Bearer-token auth on everything under `/api/*` when `OBSMCP_API_TOKEN` is set. See [`server/obsmcp_server/routers/`](server/obsmcp_server/routers/) for the exhaustive list. Highlights:

- `GET /api/stats` — counts for every entity (powers the dashboard cards).
- `GET /api/events` — SSE stream of every mutation.
- `WS /ws/dashboard` — WebSocket mirror of the same bus.
- `GET /healthz`, `/readyz`, `/runtime-discovery`, `/mode` — public.

All mutations emit a typed SSE event (`task_created`, `blocker_resolved`, `scan_completed`, …). The React client maps event → TanStack Query key and invalidates automatically; no polling.

## Development

```bash
# Backend
pytest                  # unit tests (FastAPI + TestClient against tmp SQLite)
ruff check .            # lint
mypy                    # types (best-effort)

# Frontend
cd frontend
npm install
npm run dev             # http://localhost:5173 (proxies /api to :8000)
npm run build           # emits to server/obsmcp_server/frontend_dist
npm run typecheck
```

Pre-commit hooks (`pre-commit install`) run Ruff + trailing-whitespace fixes.

## Architecture notes

- **No ORM.** The backend uses `sqlite3` directly via `threading.local()` connections. Columns that hold JSON (`tags`, `metadata`, `imports`, `exports`) are stored as JSON strings and decoded in Python.
- **IDs are UUIDs** minted client-side to allow offline-first writes.
- **Timestamps are ISO-8601 UTC** strings.
- **SSE broadcaster is thread-safe** — any handler can call `broadcast_event()` and it will fan out via `loop.call_soon_threadsafe`.
- **Graceful degradation** — if the SSE stream drops, the React app stays usable and shows an "Offline" indicator in the sidebar. Reconnection is automatic.
- **Static frontend served by the backend** — after `npm run build` the backend mounts `frontend_dist/` at `/` so a single binary serves the full app.

## Status / roadmap

Scaffolded in this PR:

- [x] Full CRUD + SSE for every entity in the spec
- [x] Local dual-mode HTTP client (SQLite + background cloud sync)
- [x] MCP stdio tool server exposing 17 tool functions
- [x] 10-page React dashboard with live SSE-driven cache invalidation
- [x] Docker image + `docker compose` deployment
- [x] Ruff + Pytest for the Python side, TypeScript typecheck + Vite build for the frontend, `docker build` for the server image (all runnable locally — no CI configured by design)

Known gaps / follow-ups:

- [ ] tree-sitter-based language parsing (currently regex heuristics)
- [ ] SQLite backup rotation
- [ ] LLM semantic descriptions are wired but opt-in; no batching/cost controls yet
- [ ] Optional GraphQL endpoint (spec marks as optional)

## License

MIT — see [`LICENSE`](LICENSE).
