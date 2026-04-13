# obsmcp Architecture

## Why this architecture

`obsmcp` now uses a centralized project-workspace architecture:

- one global control plane rooted at `C:\obsmcp` when deployed
- one isolated workspace per registered project under `projects/<project-slug>/`
- SQLite as the per-project system of record
- generated `.context` files as the universal low-friction handoff surface
- per-project Obsidian vaults as the human-readable knowledge layer
- a central hub vault for cross-project visibility
- a local MCP-compatible HTTP server as the structured integration point
- the `ctx` CLI as the fallback bridge for tools that do not speak MCP

This layout is resilient because each layer has one job and can be repaired independently.

## System layers

### 1. Project workspace model

Each project resolves to a centralized workspace:

- `projects/<project-slug>/data/db/obsmcp.sqlite3`
- `projects/<project-slug>/.context/`
- `projects/<project-slug>/vault/`
- `projects/<project-slug>/sessions/`
- `projects/<project-slug>/logs/`
- `projects/<project-slug>/project.json`

The repo stays the code source; the workspace becomes the continuity source.

### 2. System of record

SQLite stores:

- project brief sections
- current task pointer
- tasks
- work logs
- decisions
- blockers
- handoffs
- session summaries
- daily entries
- agent activity
- session labels and workstream metadata for human-readable project management

Why SQLite:

- zero external service dependency
- stable on Windows
- easy backup and inspection
- easy repair with a single file copy strategy
- enough concurrency for local multi-tool workflows

### 3. Universal continuity files

Each project workspace has its own `.context` directory, regenerated from SQLite after every write. It exists specifically for tools that:

- cannot connect to MCP
- can only read files
- can only accept pasted prompt context

This is the lowest common denominator continuity layer.

Milestone B extends this layer with cached hot-path artifacts:

- `HOT_CONTEXT.md` for fast startup
- `BALANCED_CONTEXT.md` for normal coding continuity
- `DEEP_CONTEXT.md` for architecture/debugging sessions
- `DELTA_CONTEXT.md` for "what changed since the last handoff/session"

These are regenerated during sync so MCP reads and file-based fallbacks can both stay fast.

### 4. Obsidian knowledge layer

Obsidian receives generated project notes plus daily note entries and ADR-style decision notes. Each project gets its own vault, and the hub vault summarizes all registered projects. The project vault is for:

- human review
- research capture
- debugging notes
- handoff reading
- operational memory outside the token window of any single model

### 5. MCP server

The MCP server binds locally on `127.0.0.1:9300` and routes requests into project workspaces. It exposes:

- read tools for brief, task, blockers, decisions, handoffs, notes, and status
- write tools for work logs, tasks, decisions, blockers, handoffs, daily entries, and project brief sections
- meta tools for health, listing, compact context generation, task snapshots, resume packets, recovery, project registration, and hub sync
- resource endpoints for brief, current task, handoff, status, compact context, resume packets, and project listing

Routing is continuity-aware, not just path-aware:

- explicit `project_path` and `project_slug` always win
- `session_id` and `task_id` can route later writes back into the correct project automatically
- repo bridge files and absolute file paths can be used to infer the correct project workspace when a plugin does not pass `project_path`
- `cwd` and the nearest repo root can be used as first-call routing hints when a client starts inside the project
- IDE clients can call `resolve_active_project` up front with metadata such as `cwd`, `active_file`, `workspace_folders`, `open_files`, and environment hints to get a stable project scope before the first write
- recent matching sessions can be resumed automatically on `session_open`
- `session_open` can attach a readable `session_label` and stable `workstream_key` so logs and dashboards stay understandable to humans
- `session_open` now normalizes client/model identity strings and applies a mismatch guard before auto-resuming a candidate session
- if no reliable project hint exists, continuity-sensitive MCP calls fail fast instead of silently falling back to the default project

### 6. CLI bridge

`ctx.bat` calls the same service layer as the server, so the CLI still works if the server is down. This is intentional. The continuity system should not collapse just because the MCP listener is unavailable.

It now also handles:

- project registration
- workspace path inspection
- resume packet generation
- interrupted-session recovery
- hub refresh
- startup preflight and resume-board views
- compatibility checks between client expectations and server tool schema

Server startup is intentionally lazy now:

- booting `obsmcp` does not create a default project workspace unless `bootstrap_default_project_on_startup=true`
- global `health_check` can report server readiness without creating per-project state
- project workspaces are created only when a project is explicitly registered, resolved, or otherwise used with a real project scope

## Why this is bulletproof

- Local-only by default: binds to `127.0.0.1`
- One source of truth: all writes land in SQLite first
- Atomic file writes: generated files are rewritten atomically
- Small dependency set: only `fastapi` and `uvicorn` beyond the standard library
- Restart-safe: startup and stop scripts use PID tracking and Task Scheduler integration
- Per-project durability: sessions also write metadata, heartbeat, worklog, and handoff files into `sessions/<session-id>/`
- Session reuse: reopened tools can resume recent matching sessions instead of spawning unnecessary parallel sessions
- Human-readable session management: labels and workstreams make open/closed session history legible without decoding session ids
- Safer startup: preflight warnings and resume boards expose stale sessions, done current tasks, handoff mismatches, and taskless substantive work
- Recovery-aware audits: stale and abandoned sessions are flagged so another model can recover them cleanly
- Easy inspection: every important artifact is just SQLite, JSON, Markdown, batch, or Python
- Vendor-neutral: MCP, file reads, CLI, and prompt injection all work from the same state

## Why this is token-efficient

- project workspaces keep compact current-state files instead of full note dumps
- `generate_compact_context` creates a short prompt-ready summary
- `generate_context_profile` assembles tiered `fast`, `balanced`, `deep`, `handoff`, and `recovery` context variants from the same state
- `generate_delta_context` lets the next model read only what changed since the previous reference point
- `generate_resume_packet` creates a first-read handoff packet for the next model
- relevant files are tracked explicitly
- recent work is bounded
- decisions and blockers are summarized rather than replaying full history
- handoffs are auto-enriched with task state, relevant files, and semantic suggestions instead of relying only on long freeform prose

## Output-token policy layer

Output-token reduction is implemented as a separate response-policy layer and not as part of the continuity stack.

This separation is deliberate:

- continuity, handoffs, logs, delta context, retrieval context, prompt segments, and semantic caches keep their original fidelity
- output-token savings target only model-generated prose
- post-generation compaction remains auxiliary because it does not save model output tokens that were already generated

The control plane lives in `config/obsmcp.json` under `output_compression` and supports:

- `off`
- `prompt_only`
- `gateway_enforced`

Current enforcement scope is intentionally narrow and honest:

- `prompt_only` and `gateway_enforced` affect `generate_startup_prompt_template`
- `gateway_enforced` also affects LLM-backed semantic descriptions by appending the enforced response contract to the OpusMax text-provider system prompt
- task overrides and safety bypass rules are resolved before contract generation so review/debugging/architecture tasks can use different brevity styles without touching context assembly

This means `obsmcp` saves output tokens only where it actually owns the generation boundary today, while preserving the existing input-token-saving architecture intact.

## Milestone B performance hardening

Milestone B adds a dedicated context artifact cache in SQLite plus generated workspace files. The service now:

- computes a project-local state version from task/work/blocker/handoff/session changes
- caches tiered context artifacts keyed by profile, task scope, and token budget
- reuses cached context when the project state has not changed
- writes fresh tiered context files into `.context` and `data/json` during sync
- exposes a delta view so resumed agents do not need to replay unchanged project history

This improves latency without sacrificing continuity because the underlying source of truth remains SQLite.

## Why this is especially strong for cross-model continuity

When one model stops halfway, the next model can recover from any of these layers:

- MCP tools: structured read of task, blockers, handoff, decisions, files
- per-project `.context`: immediate file-based continuity with no protocol support required
- per-project session folders: metadata, heartbeat timeline, worklog, resume packet, and emergency handoff files
- Obsidian project vault: human-readable history and architecture notes
- hub vault: cross-project visibility and quick switching
- CLI: quick manual updates and state sync from any shell-capable tool

The key design choice is that continuity is not attached to one client. It is attached to `obsmcp`.
