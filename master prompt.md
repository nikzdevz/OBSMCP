# Master Prompt

Use `obsmcp` as the primary continuity system for this project.

MCP endpoint:

```text
http://127.0.0.1:9300/mcp
```

You must treat `obsmcp` as the shared memory and continuity layer for this project. Do not rely on the full prior chat history as your default memory source. The current user request is the source of truth for the immediate task, the codebase is the source of truth for implementation details, and `obsmcp` is the source of truth for continuity, handoffs, blockers, decisions, and recent work.

## Core policy

You must do all of the following:

1. Read continuity state from `obsmcp` before starting substantive work.
2. Open a tracked session in `obsmcp`.
3. Log meaningful progress to `obsmcp` during the session.
4. Record blockers, decisions, and task changes in `obsmcp`.
5. Create a handoff in `obsmcp` before ending the session.
6. Close the session in `obsmcp` with a summary.
7. If MCP write-back fails, say so explicitly and fall back to the `.context` files.

Do not use full historical chat replay as your primary working memory. Only consult older chat if `obsmcp` is unavailable, stale, or missing critical context.

## Required startup sequence

At the start of this conversation, do the following in order:

1. Call `health_check`.
2. Call `get_project_status_snapshot`.
3. Call `get_current_task`.
4. Call `get_latest_handoff`.
5. Call `get_blockers`.
6. Call `get_relevant_files`.
7. Call `generate_compact_context`.
8. Call `generate_context_profile(profile="fast" or "balanced")`.
9. Call `generate_delta_context`.
10. If you need targeted understanding of specific files, functions, classes, or features, use:
   - `describe_module`
   - `describe_symbol`
   - `describe_feature`
   - `search_code_knowledge`
11. Call `session_open`.

If this is the first contact with the project, also:

1. Understand the codebase structure.
2. Check if a **Code Atlas** exists for this project. Call `get_code_atlas_status()`. If `exists: false`, immediately call `scan_codebase()` to generate it. The atlas gives you a structural understanding of every file, function, class, and feature — it is the fastest way to understand a project without reading every source file.
3. Summarize the codebase in a compact way.
4. Create or update the current task if needed.
5. Log an initial work entry describing what you learned.

## Required session_open contract

Open the session with this API shape:

```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "method": "tools/call",
  "params": {
    "name": "session_open",
    "arguments": {
      "actor": "<agent-or-plugin-name>",
      "client_name": "<client-name>",
      "model_name": "<model-name>",
      "project_path": "<absolute-project-path>",
      "initial_request": "<user-request-for-this-session>",
      "session_goal": "<what-you-plan-to-accomplish>",
      "task_id": "<task-id-if-known>",
      "require_heartbeat": true,
      "require_work_log": true,
      "heartbeat_interval_seconds": 900,
      "work_log_interval_seconds": 1800,
      "min_work_logs": 1,
      "handoff_required": true
    }
  }
}
```

Store the returned `session_id` and reuse it in all later write operations.

## Required read tools

Use these tools for continuity reads:

- `get_project_status_snapshot`
- `get_current_task`
- `get_active_tasks`
- `get_latest_handoff`
- `get_recent_work`
- `get_decisions`
- `get_blockers`
- `get_relevant_files`
- `search_notes`
- `read_note`
- `generate_compact_context`
- `generate_context_profile`
- `generate_delta_context`
- `generate_task_snapshot`
- `get_active_sessions`
- `detect_missing_writeback`
- `get_code_atlas_status` — check if the project has a Code Atlas
- `scan_codebase` — generate the Code Atlas (only needed if atlas doesn't exist or is stale)
- `describe_module` — fetch a cached or fresh semantic description for a file/module
- `describe_symbol` — fetch a cached or fresh semantic description for a function/class
- `describe_feature` — fetch a feature-level explanation across files
- `search_code_knowledge` — search semantic knowledge instead of rereading large notes
- `get_scan_job` / `wait_for_scan_job` — poll background atlas scans when `scan_codebase` returns a job instead of a finished atlas
- `get_symbol_candidates` — disambiguate duplicate symbol names
- `get_related_symbols` — expand from one symbol to nearby or feature-related symbols
- `get_task_templates` — list available task templates
- `get_audit_log` — full activity timeline
- `get_log_stats` — work log statistics by age
- `get_blocked_tasks` — tasks blocked by unresolved dependencies
- `validate_dependencies` — check for circular/broken task dependencies
- `get_all_dependencies` — full dependency map

## Required write behavior

You must write to `obsmcp` when any of these happen:

- you begin meaningful work on the task
- you finish a meaningful chunk
- you discover a blocker
- you make a decision
- you update the task scope or task status
- you identify important relevant files
- you are about to stop, hand off, or switch tools
- you want to use bulk_task_ops for multiple task changes at once (more efficient than individual calls)
- you complete a sprint or milestone and want to export the full project state (use `export_project`)
- you want to archive old work logs to keep context bounded (use `expire_old_logs` with configured retention)

Do not wait until the very end to write everything.

## Required write formats

### Log progress

Use `log_work`:

```json
{
  "name": "log_work",
  "arguments": {
    "actor": "<agent-name>",
    "session_id": "<session-id>",
    "task_id": "<task-id>",
    "message": "<clear progress statement>",
    "summary": "<short compact summary if useful>",
    "files": ["<file1>", "<file2>"]
  }
}
```

Use `log_work` for:

- code understanding summaries
- implementation progress
- bug investigation findings
- test results
- refactor progress
- ingestion summaries on first project contact
- semantic knowledge generation results when you discover important modules, features, or symbol behaviors

### Create or update tasks

Use `create_task` when a new actionable unit of work is discovered.

```json
{
  "name": "create_task",
  "arguments": {
    "actor": "<agent-name>",
    "session_id": "<session-id>",
    "title": "<task title>",
    "description": "<task description>",
    "priority": "low|medium|high",
    "owner": "<optional-owner>",
    "relevant_files": ["<file1>", "<file2>"],
    "tags": ["<tag1>", "<tag2>"]
  }
}
```

Use `update_task` when scope or status changes.

```json
{
  "name": "update_task",
  "arguments": {
    "task_id": "<task-id>",
    "actor": "<agent-name>",
    "session_id": "<session-id>",
    "status": "open|in_progress|blocked|done",
    "description": "<updated-description-if-needed>",
    "priority": "low|medium|high",
    "relevant_files": ["<file1>", "<file2>"],
    "tags": ["<tag1>", "<tag2>"]
  }
}
```

Use `set_current_task` whenever you know the active task:

```json
{
  "name": "set_current_task",
  "arguments": {
    "task_id": "<task-id>",
    "actor": "<agent-name>",
    "session_id": "<session-id>"
  }
}
```

### Log decisions

Use `log_decision` for architectural, tooling, debugging, or implementation decisions.

```json
{
  "name": "log_decision",
  "arguments": {
    "actor": "<agent-name>",
    "session_id": "<session-id>",
    "task_id": "<task-id>",
    "title": "<decision title>",
    "decision": "<final decision>",
    "rationale": "<why this was chosen>",
    "impact": "<effect on the project>"
  }
}
```

### Log blockers

Use `log_blocker` immediately when progress is blocked.

```json
{
  "name": "log_blocker",
  "arguments": {
    "actor": "<agent-name>",
    "session_id": "<session-id>",
    "task_id": "<task-id>",
    "title": "<blocker title>",
    "description": "<what is blocked and why>"
  }
}
```

When the blocker is resolved, use `resolve_blocker`.

### Handoffs

Before ending the session, create a handoff:

```json
{
  "name": "create_handoff",
  "arguments": {
    "from_actor": "<agent-name>",
    "to_actor": "<next-agent-or-tool>",
    "session_id": "<session-id>",
    "task_id": "<task-id>",
    "summary": "<what was completed and current state>",
    "next_steps": "<what the next agent should do next>",
    "open_questions": "<remaining questions or risks>",
    "note": "<extra implementation details worth preserving>"
  }
}
```

The handoff must be good enough that another model can continue without re-explaining the project.

## Heartbeat policy

If the session stays open for a while, call `session_heartbeat`.

Use this shape:

```json
{
  "name": "session_heartbeat",
  "arguments": {
    "session_id": "<session-id>",
    "actor": "<agent-name>",
    "status_note": "<short current status>",
    "task_id": "<task-id-if-known>",
    "files": ["<file1>", "<file2>"],
    "create_work_log": true
  }
}
```

Rules:

- heartbeat at least every 15 minutes during long sessions
- if meaningful progress happened, set `create_work_log` to `true`
- do not leave long silent stretches with no write-back

## Session close policy

Before ending the conversation or stopping work, call `session_close`.

Use this shape:

```json
{
  "name": "session_close",
  "arguments": {
    "session_id": "<session-id>",
    "actor": "<agent-name>",
    "summary": "<compact final summary of this session>",
    "create_handoff": true,
    "handoff_summary": "<what the next model should know>",
    "handoff_next_steps": "<ordered next steps>",
    "handoff_open_questions": "<open questions>",
    "handoff_note": "<important implementation note>",
    "handoff_to_actor": "<next-agent-or-tool>"
  }
}
```

Do not close the session silently without a handoff unless the user explicitly says no continuity write-back is needed.

## First-time project ingestion rule

If this is the first time you are seeing the project, you must:

1. Inspect the codebase structure.
2. Identify major modules, entry points, configs, tests, and risks.
3. Create or update the active task.
4. Log at least one `log_work` entry summarizing the codebase understanding.
5. Record important files with the task or work log.
6. Record a decision only if a real decision was made.
7. Create a handoff before ending, even if the work was only discovery.

## Bug-fixing rule

If the task is bug related, you must log:

- what the bug is
- where it appears
- what files are involved
- what hypothesis you tested
- what fix you applied or why it remains blocked

Minimum expected write-back for bug work:

1. `log_work` when bug analysis starts
2. `log_blocker` if reproduction or root cause is blocked
3. `log_decision` if a fix strategy is chosen
4. `log_work` after fix or test result
5. `create_handoff` before exit

## If MCP fails

If MCP read or write fails:

1. Say clearly that MCP failed.
2. Read these fallback files:
   - `.context/PROJECT_CONTEXT.md`
   - `.context/CURRENT_TASK.json`
   - `.context/HANDOFF.md`
   - `.context/DECISIONS.md`
   - `.context/BLOCKERS.json`
   - `.context/RELEVANT_FILES.json`
   - `.context/SESSION_SUMMARY.md`
   - `.context/SESSION_AUDIT.json`
3. Continue work using those files.
4. If shell access exists, use `ctx.bat`.
5. If neither MCP nor CLI write-back is available, say that continuity write-back could not be completed.

If `scan_codebase` returns a queued or running background job instead of a finished atlas:

1. store the returned `job_id`
2. poll `get_scan_job(job_id)` until the status is `completed`, `failed`, or `interrupted`
3. if your client supports longer waits, call `wait_for_scan_job(job_id, wait_seconds=30+)`
4. once completed, continue with `get_code_atlas_status`, `describe_module`, `describe_symbol`, or `search_code_knowledge`

## CLI fallback

If MCP tool calls are unavailable but shell access exists, use:

```bat
ctx.bat session open --actor "<agent>" --client "<client>" --model "<model>" --project-path "<path>" --initial-request "<request>" --goal "<goal>"
ctx.bat log "message" --task TASK-ID --actor "<agent>"
ctx.bat decision log "title" --decision "decision text" --task TASK-ID --actor "<agent>"
ctx.bat handoff --summary "summary" --next-steps "next steps" --open-questions "questions" --to "next-agent"
ctx.bat session close SESSION-ID --actor "<agent>" --summary "summary"
ctx.bat audit
```

## Final operating rule

You are not just answering the user. You are maintaining continuity for the next model too.

Every meaningful session must leave behind:

- current task state
- recent progress
- decisions
- blockers
- relevant files
- recommended semantic lookups when specific modules/symbols/features matter for the next model
- handoff
- closed session summary

If you cannot perform any of that, you must say so explicitly.
