# Contributing

Thanks for helping improve OBSMCP!

## Local setup

```bash
python -m pip install -e ".[dev]"
pre-commit install

cd frontend && npm install
```

## Running the stack locally

```bash
# Terminal 1 — backend
OBSMCP_API_TOKEN=dev obsmcp-server

# Terminal 2 — frontend
cd frontend
npm run dev     # http://localhost:5173 (proxies /api and /ws to 8000)

# Terminal 3 — local agent (writes to the running backend)
./start.sh      # first run prompts for config
```

## Code style

- **Python:** formatted + linted by `ruff` (see `pyproject.toml`). 100-char lines.
- **TypeScript:** strict mode on; lint with `npm run lint`.
- **No ORMs.** Stick to raw `sqlite3`.
- **Every mutation must emit an SSE event** via `broadcast_event(...)`. See existing routers.

## Adding a new entity

1. Extend `server/obsmcp_server/schema.sql`.
2. Add a router under `server/obsmcp_server/routers/`, hook it up in `main.py`, and emit events.
3. Add a TypeScript type in `frontend/src/api/types.ts`.
4. Map event types → query keys in `frontend/src/events/EventBus.ts`.
5. Add a page under `frontend/src/pages/`, wire it into `App.tsx` and the sidebar nav.
6. Mirror the write path in `tool/obsmcp/client/http_client.py` so agents can use it offline-first.
7. Add tests in `server/tests/` and `tool/tests/`.

## Tests

```bash
pytest -q                 # backend + tool
cd frontend && npm run typecheck && npm run build
```

Run all three locally before opening a PR — this project intentionally has no CI/GitHub Actions.
