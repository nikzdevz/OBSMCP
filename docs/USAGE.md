# Usage Guide

## Daily operator flow

1. Start `obsmcp`
2. Register the project repo if it has not been seen before
3. Open or resolve the centralized project workspace
4. Open a tracked session with `ctx` or MCP `session_open`
5. Create or select the current task with `ctx`
6. Use semantic lookups for targeted understanding before rereading large files
7. Log work as you go
8. Heartbeat long sessions
9. Record blockers and decisions when they happen
10. Create a handoff before switching models or stopping
11. Close the session and let the next tool read the project workspace `.context`, session folder, or query MCP

## Automatic vs manual continuity behavior

What is automatic now:

- opening the same repo in another IDE or plugin generally resolves to the same `obsmcp` workspace
- `obsmcp` can infer project routing from `project_path`, `repo_path`, repo bridge files, and absolute file paths passed in tool arguments
- `obsmcp` can also infer project routing from `session_id`, `task_id`, `cwd`, and the nearest repo root when the client starts inside a project
- plugins can now call `resolve_active_project` with IDE metadata such as `cwd`, `active_file`, `workspace_folders`, `open_files`, or `env_variables` before their first write
- `session_open` defaults to `resume_strategy=auto`, so reopening the same actor/client/project usually resumes the recent open session instead of creating another one
- `session_open` now derives a readable `session_label` and stable `workstream_key`, or accepts them directly from the client
- `session_open` now normalizes client/model identity values and blocks unsafe auto-resume when the incoming request conflicts with the candidate session
- `session_close` now auto-enriches handoffs with relevant files, task state, and recommended semantic lookups even when only a short summary is provided

What is still client-dependent:

- whether the tool actually performs MCP write-back during work
- whether the tool updates relevant files or handoffs without being prompted
- whether the client includes a project hint on the first continuity-sensitive MCP call when it is not running inside the repo

When a client does not write reliably, use `ctx.bat` as the fallback bridge.
When a client does not provide any usable project hint, `obsmcp` now rejects continuity-sensitive MCP calls with a clear error instead of silently writing into the default project.

## Recommended startup guardrail flow

Use this sequence before meaningful work:

1. `ctx.bat --project D:\Work\myapp status`
2. `ctx.bat --project D:\Work\myapp preflight --actor codex --initial-request "..." --goal "..."`
3. `ctx.bat --project D:\Work\myapp resume-board`
4. Create or select the task
5. Open the session with a label and workstream when possible

This catches the most common continuity problems before the model starts:

- stale or abandoned sessions
- current task already marked done
- latest handoff belonging to another task
- substantial session startup with no task attached
- unsafe auto-resume candidates

## Project workspace examples

Register a repo:

```bat
ctx.bat project register --repo D:\Work\myapp --name "My App" --tags python,fastapi
```

Inspect the centralized workspace paths:

```bat
ctx.bat project paths --repo D:\Work\myapp
```

Migrate older repo-local `.context` / `obsidian\vault` content:

```bat
ctx.bat project migrate --repo D:\Work\myapp
```

Generate a resume packet for the next model:

```bat
ctx.bat --project D:\Work\myapp resume
```

Generate cached tiered context profiles:

```bat
ctx.bat --project D:\Work\myapp compact --profile fast --max-tokens 1200
ctx.bat --project D:\Work\myapp compact --profile balanced --max-tokens 2500
ctx.bat --project D:\Work\myapp compact --profile deep --max-tokens 4500 --daily-notes
```

Generate a delta view since the latest handoff or a specific session:

```bat
ctx.bat --project D:\Work\myapp delta
ctx.bat --project D:\Work\myapp delta --session SESSION-REPLACE-ME
ctx.bat --project D:\Work\myapp delta --handoff 42
```

Queue a background Code Atlas scan and wait for it:

```bat
ctx.bat --project D:\Work\myapp atlas generate --background
ctx.bat --project D:\Work\myapp atlas jobs
ctx.bat --project D:\Work\myapp atlas wait SCAN-REPLACE-ME --wait-seconds 60
```

Recover an interrupted session:

```bat
ctx.bat --project D:\Work\myapp recover --session SESSION-REPLACE-ME --actor claude-recovery
```

Refresh the central hub vault:

```bat
ctx.bat hub sync
```

## Common CLI examples

Create a task:

```bat
ctx.bat --project D:\Work\myapp task create "Implement auth cache" --description "Add a local token cache for provider adapters" --files server/main.py,server/service.py
```

Set current task:

```bat
ctx.bat --project D:\Work\myapp start TASK-12345678-implement-auth
```

Log work:

```bat
ctx.bat --project D:\Work\myapp log "Added cache invalidation path" --task TASK-12345678-implement-auth --files server/service.py,tests/test_service.py
```

Describe a module:

```bat
ctx.bat --project D:\Work\myapp describe module server\service.py
```

Describe a symbol:

```bat
ctx.bat --project D:\Work\myapp describe symbol generate_resume_packet --module server\service.py --type function
```

Search semantic knowledge:

```bat
ctx.bat --project D:\Work\myapp knowledge search "resume packet"
```

Open a session:

```bat
ctx.bat session open --actor codex --client vscode-codex --model gpt-5 --project-path D:\Work\myapp --initial-request "Understand the codebase and continue the current task" --goal "Preserve continuity for the next model"
```

Open a named session inside a stable workstream:

```bat
ctx.bat session open --actor claude-code --client claude-code-vscode --model claude-opus-4-6 --project-path D:\Work\myapp --task TASK-12345678-docs --label "Managing Director Email" --workstream managing-director-email --initial-request "This task is for the managing director's email." --goal "Draft and finalize the message"
```

Force a brand-new session instead of auto-resuming:

```bat
ctx.bat session open --actor codex --client vscode-codex --model gpt-5 --project-path D:\Work\myapp --resume-strategy new
```

Run startup safety checks before opening or resuming:

```bat
ctx.bat --project D:\Work\myapp preflight --actor codex --initial-request "Create the ERP documentation" --goal "Write the beginner guide"
```

Show the startup resume board:

```bat
ctx.bat --project D:\Work\myapp resume-board
```

Check client/server compatibility:

```bat
ctx.bat compat --client claude-code --model opus-4.6 --client-api-version 2026.04.14 --client-tool-schema-version 2
```

Resume a specific previous session directly:

```bat
ctx.bat session open --actor codex --client vscode-codex --model gpt-5 --project-path D:\Work\myapp --resume-strategy resume --resume-session-id SESSION-REPLACE-ME
```

Heartbeat a session:

```bat
ctx.bat session heartbeat SESSION-REPLACE-ME --actor codex --note "Still tracing the auth path" --files server/service.py,server/store.py --create-work-log
```

Log a blocker:

```bat
curl -X POST http://127.0.0.1:9300/mcp ^
  -H "Content-Type: application/json" ^
  -d "{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"tools/call\",\"params\":{\"name\":\"log_blocker\",\"arguments\":{\"title\":\"Missing API contract\",\"description\":\"Need final response shape from upstream client\",\"task_id\":\"TASK-12345678-implement-auth\",\"actor\":\"codex\"}}}"
```

Create a handoff:

```bat
ctx.bat handoff --summary "Cache path is in place; tests still need edge-case coverage." --next-steps "Add concurrency tests; verify stale token eviction." --open-questions "Should cache be shared across workspaces?" --to "claude-code"
```

Close the session:

```bat
ctx.bat session close SESSION-REPLACE-ME --actor codex --summary "Completed cache implementation and left handoff." --handoff-summary "Cache logic is in place and synced."
```

Audit continuity:

```bat
ctx.bat --project D:\Work\myapp audit
```

Verify a reset actually left the workspace clean:

```bat
curl -X POST http://127.0.0.1:9300/mcp ^
  -H "Content-Type: application/json" ^
  -d "{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"tools/call\",\"params\":{\"name\":\"reset_project\",\"arguments\":{\"scope\":\"full\",\"actor\":\"codex\",\"project_path\":\"D:\\Work\\myapp\"}}}"
```

The `reset_project` response now includes `post_reset_snapshot` so you can immediately verify:

- `current_task`
- `active_tasks`
- `latest_handoff`
- `recent_work`
- `active_sessions`

## MCP usage pattern

The recommended read order for any MCP-capable client is:

1. `get_project_status_snapshot`
2. `get_current_task`
3. `get_latest_handoff`
4. `get_blockers`
5. `generate_context_profile(profile="fast"|"balanced")`
6. `generate_delta_context`
7. `describe_module` / `describe_symbol` / `describe_feature` when you need targeted semantic understanding

Recommended profile usage:

- `fast`: lowest-latency startup, ideal for small edits or quick continuation
- `balanced`: default day-to-day context for normal coding sessions
- `deep`: more history, dependencies, and notes for debugging or architecture work
- `handoff`: same continuity surface, but biased toward transition quality
- `recovery`: same continuity surface, but includes audit-heavy recovery cues

## Output response policy

The `output_compression` block in `config/obsmcp.json` controls output-token behavior without changing continuity or context generation.

Modes:

- `off`: disable concise-response policy
- `prompt_only`: append a concise-response contract where `obsmcp` emits prompt text
- `gateway_enforced`: apply a stricter response contract where `obsmcp` directly controls model generation

Current generation surfaces:

- `generate_startup_prompt_template`
- LLM-backed semantic descriptions such as `describe_module`, `describe_symbol`, and `describe_feature` when they use the OpusMax text provider

Inspect the effective policy for a task or operation:

```bat
curl -X POST http://127.0.0.1:9300/mcp ^
  -H "Content-Type: application/json" ^
  -d "{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"tools/call\",\"params\":{\"name\":\"get_output_response_policy\",\"arguments\":{\"operation_kind\":\"review\",\"project_path\":\"D:\\Work\\myapp\"}}}"
```

Rollout order:

1. start with `prompt_only`
2. validate output quality for your real tasks
3. move selected tasks to `gateway_enforced`
4. keep dangerous/security-sensitive flows on safety bypass or `off`

For large repos, prefer this scan workflow:

1. call `scan_codebase`
2. if the response status is `queued` or `running`, poll `get_scan_job`
3. optionally call `wait_for_scan_job` when the client supports longer polling windows
4. continue with `get_code_atlas_status`, `describe_module`, `describe_symbol`, or `search_code_knowledge` after the scan completes

The recommended write pattern is:

1. `session_open`
2. `log_work`
3. `log_decision` or `log_blocker` when needed
4. `session_heartbeat` during long work
5. `create_handoff` before exit or model switch
6. `session_close`

Recommended first-call pattern for IDE clients and plugins:

1. call `resolve_active_project` with `project_path` if known
2. otherwise pass `cwd`, `active_file`, `workspace_folders`, `open_files`, or IDE env hints
3. if the response is resolved, reuse the returned `project_path` for later calls
4. if the response says `requires_registration`, retry with `auto_register=true` or register explicitly
5. if the response is unresolved, ask the user for the project or wait until the client has a usable IDE hint

Notes:

- `session_open` now prefers reusing a recent matching open session for the same actor/client/project
- explicit `session_label` and `workstream_key` are recommended whenever the client can infer a named workstream from the user's prompt
- `get_startup_preflight` and `get_resume_board` are the preferred startup reads for IDE clients deciding between resume vs new work
- if a substantial request starts without `task_id`, `obsmcp` warns instead of silently treating the session as untracked work
- `session_close` will generate a richer handoff automatically if you do not provide every handoff field manually
- `bootstrap_default_project_on_startup=false` is now the recommended default, so the server starts empty and waits for an explicit project resolution instead of recreating a default project workspace during boot
- if a client only passes absolute file paths, `obsmcp` can often still route the write to the correct project workspace
- if the client is inside the repo, `session_open` and other continuity tools can route from `cwd` automatically
- if there is no reliable project signal at all, continuity-sensitive MCP calls fail and ask the client to pass `project_path` or another project hint first
- `sync_context_files` now also refreshes `.context/HOT_CONTEXT.md`, `.context/BALANCED_CONTEXT.md`, `.context/DEEP_CONTEXT.md`, and `.context/DELTA_CONTEXT.md`

## Multi-tool continuity pattern

### VS Code + Codex

- start with `.context/PROJECT_CONTEXT.md`
- read `.context/CURRENT_TASK.json`
- query MCP if direct integration is available
- update state with `ctx.bat` or MCP tools

### VS Code + Claude-style agent

- open `.context` first
- read `AGENTS.md` and `CLAUDE.md`
- use `ctx.bat` when MCP is not directly available
- create a handoff before ending the session

### Warp.dev

- use `ctx.bat status`, `ctx.bat current`, and `ctx.bat log`
- paste the output of `ctx.bat status`, `generate_compact_context`, or `ctx.bat knowledge search ...` into the chat when needed

### Cursor or similar IDE agents

- point the agent to `.context`
- better: point it to the centralized project workspace `.context`
- if MCP config is supported, target `http://127.0.0.1:9300/mcp`
- otherwise use `ctx.bat` plus manual prompt injection

### Manual-only tools

- paste `.context/PROJECT_CONTEXT.md`
- paste `.context/HANDOFF.md`
- paste `.context/CURRENT_TASK.json`
- paste `master prompt.md`
- instruct the model to preserve continuity and write a new handoff before stopping
