# obsmcp

<p align="center">
  <strong>OBS MCP / Obsidian MCP for serious project continuity, multi-session AI work, and developer-grade context engineering.</strong>
</p>

<p align="center">
  <img alt="Windows First" src="https://img.shields.io/badge/platform-Windows%20first-0f172a">
  <img alt="Transport" src="https://img.shields.io/badge/MCP-HTTP%20%2B%20CLI-2563eb">
  <img alt="Backend" src="https://img.shields.io/badge/backend-FastAPI-059669">
  <img alt="Storage" src="https://img.shields.io/badge/storage-SQLite%20%2B%20Markdown-7c3aed">
  <img alt="Continuity" src="https://img.shields.io/badge/focus-Project%20continuity-f59e0b">
</p>

`obsmcp` stands for `Obsidian MCP`.

It is a local-first MCP server and continuity control plane that helps AI coding tools keep working memory between sessions, models, IDEs, and interruptions without turning your chat history into the only source of truth.

Instead of relying on one long conversation, `obsmcp` stores:

- project state in SQLite
- compact, prompt-friendly continuity files in `.context`
- human-readable notes and handoffs in Obsidian vaults
- auditable session history, task history, and model-to-model handoffs
- code-aware semantic knowledge through a Code Atlas and semantic lookup layer

If you are building with Codex, Claude Code, Cursor, Warp, VS Code MCP clients, or your own internal tooling, `obsmcp` is designed to be the shared memory and project-management layer those tools can all use together.

## Table Of Contents

- [What Is OBS MCP?](#what-is-obs-mcp)
- [Why It Exists](#why-it-exists)
- [What Makes It Powerful](#what-makes-it-powerful)
- [Architecture At A Glance](#architecture-at-a-glance)
- [Feature Inventory](#feature-inventory)
- [How Token Saving Works](#how-token-saving-works)
- [Installation](#installation)
- [Quick Start](#quick-start)
- [MCP Tool Catalog](#mcp-tool-catalog)
- [Comparison With Other MCP Servers](#comparison-with-other-mcp-servers)
- [Where obsmcp Wins](#where-obsmcp-wins)
- [Where obsmcp Is Weaker](#where-obsmcp-is-weaker)
- [Documentation Index](#documentation-index)

## What Is OBS MCP?

`obsmcp` is not just another MCP tool server.

It is a project operations layer for AI-assisted development:

- it knows what project is active
- it knows what task is current
- it knows what was done recently
- it knows what is blocked
- it knows what should happen next
- it knows which files matter
- it knows when a session is stale or abandoned
- it can produce compact startup context instead of replaying long history

You can think of it as a hybrid of:

- a continuity server
- a local project memory system
- a task and handoff tracker
- a prompt-context engineering layer
- a semantic code knowledge service
- an MCP gateway for selected external tools

## Why It Exists

Most MCP servers are excellent at one narrow job:

- file access
- browser automation
- GitHub automation
- memory search
- code execution

Those are useful, but they do not solve the bigger operational problem:

> When an AI model stops, switches, crashes, resumes, or hands off work, how does the next model continue the project cleanly?

That is the problem `obsmcp` is built to solve.

It gives you:

- project-scoped memory instead of chat-scoped memory
- structured tasks, blockers, and decisions instead of loose notes
- safe startup checks before a model resumes the wrong thing
- resumable sessions with labels and workstreams
- token-aware context surfaces for fast restart
- human-readable notes for debugging and handoff review

## What Makes It Powerful

### 1. Continuity is attached to the project, not to one chat

`obsmcp` keeps state in a centralized workspace per project, so the next model or IDE can continue from the actual project state.

### 2. It supports real project management

It tracks:

- tasks
- current task
- blockers
- decisions
- work logs
- handoffs
- sessions
- dependencies
- recovery state

### 3. It is built for multi-session, multi-model work

With session labels, workstreams, preflight checks, resume boards, and mismatch guards, `obsmcp` is designed for interrupted and branchy work instead of one perfect uninterrupted run.

### 4. It is optimized for token efficiency

It does not only store memory. It helps shape what the next model sees:

- fast context
- balanced context
- deep context
- handoff context
- recovery context
- delta context
- prompt segments
- retrieval context
- raw-output compaction
- output-response policy controls

### 5. It is code-aware

The Code Atlas + semantic layer means tools can ask:

- what this module does
- what this function does
- what features exist
- which symbols are related
- what changed since the last handoff

That is very different from a plain memory bank.

## Architecture At A Glance

```text
Developer / IDE / AI Client
        |
        v
  MCP / CLI / File Reads
        |
        v
      obsmcp
        |
        +--> SQLite project state
        +--> .context continuity files
        +--> per-project Obsidian vault
        +--> session folders and handoffs
        +--> semantic code atlas
        +--> optional provider-backed tools
```

### Core layers

| Layer | Purpose |
| --- | --- |
| SQLite | System of record for tasks, sessions, blockers, decisions, logs, handoffs, metrics |
| `.context` | Universal fallback surface for tools that cannot call MCP directly |
| Obsidian vault | Human-readable operational memory and project notes |
| Session folders | Durable artifacts like metadata, heartbeat history, worklog, and handoff files |
| Code Atlas | File/function/class/feature understanding across the repository |
| MCP server | Structured tool access over HTTP |
| `ctx.bat` CLI | Shell fallback when MCP integration is unavailable or inconvenient |

### Workspace model

Each project gets its own centralized workspace under:

```text
projects/<project-slug>/
```

With subdirectories such as:

- `data/db/`
- `.context/`
- `vault/`
- `sessions/`
- `logs/`

## Feature Inventory

### Project continuity

- centralized project workspace per repo
- project registration and routing
- repo bridge attachment for path inference
- current-task tracking
- relevant-file tracking
- model-to-model handoffs
- daily note stream
- audit trail

### Session management

- auditable session open / heartbeat / close lifecycle
- session labels for human-readable tracking
- stable workstream keys for related sessions
- startup preflight warnings
- startup resume board
- session mismatch guard for unsafe auto-resume
- stale-session and abandoned-session detection
- emergency recovery handoffs
- session lineage

### Context engineering

- compact context
- token-budget-aware compact context v2
- tiered profiles: `fast`, `balanced`, `deep`, `handoff`, `recovery`
- delta context since handoff/session/timestamp
- retrieval context
- startup context
- prompt segments for cache-friendly assembly
- progressive chunked context loading

### Token and output optimization

- token usage metrics
- raw tool-output capture
- noisy command-output compaction
- output-response policy
- operation-aware optimization policy
- fast-path deterministic responses

### Code understanding

- full codebase scan / Code Atlas
- semantic module descriptions
- semantic symbol descriptions
- feature descriptions
- related-symbol expansion
- semantic search
- background scan jobs

### Developer operations

- command-event recording and replay
- command risk classification
- task templates
- bulk task operations
- dependency management
- log retention / expiry
- project export

### External tool gateway

- web search
- image understanding

## How Token Saving Works

This is one of the biggest differences between `obsmcp` and many memory-oriented MCP servers.

`obsmcp` tries to save tokens at multiple levels:

### Input-token savings

- use `generate_fast_context` or `generate_context_profile("fast")` for minimal startup context
- use `generate_delta_context` to send only what changed instead of replaying full history
- use `generate_retrieval_context` for targeted context instead of large note dumps
- use semantic lookups instead of rereading giant files
- use prompt segments for cache-friendly context assembly

### Output-token savings

- `compact_tool_output`
- `compact_response`
- `get_output_response_policy`
- `generate_startup_prompt_template`
- gateway-enforced response style on the surfaces `obsmcp` actually controls

### What `obsmcp` does better than basic memory servers

Many memory servers reduce repeated context by storing facts. `obsmcp` does that kind of work too, but it also helps decide:

- what to show now
- how much to show
- which context tier to use
- how to avoid replaying unchanged state
- how to compress verbose tool output safely

### What it does not claim

`obsmcp` does not magically reduce every token in every client. Output savings only happen on the surfaces it controls directly. If a client ignores the optimized context or bypasses its response policies, those savings can be reduced.

## Installation

## Prerequisites

- Windows
- Python `3.11+`
- PowerShell or Command Prompt
- Obsidian installed locally if you want live vault-based workflows

## Recommended install path

```text
C:\obsmcp
```

This keeps the batch scripts and Task Scheduler paths simple.

## Install

```bat
git clone https://github.com/<your-org>/obsmcp.git C:\obsmcp
cd /d C:\obsmcp
bootstrap_obsmcp.bat
```

What `bootstrap_obsmcp.bat` does:

- creates `.venv`
- upgrades `pip`
- installs Python dependencies from `requirements.txt`

## Start the server

```bat
start_obsmcp.bat
```

The server starts locally on:

```text
http://127.0.0.1:9300
```

## Stop the server

```bat
stop_obsmcp.bat
```

## Verify health

```bat
curl http://127.0.0.1:9300/healthz
netstat -ano | findstr :9300
ctx.bat project list
```

## Optional local API token

```bat
set OBSMCP_API_TOKEN=your-local-token
```

## MCP client configuration

```json
{
  "mcpServers": {
    "obsmcp": {
      "transport": "http",
      "url": "http://127.0.0.1:9300/mcp"
    }
  }
}
```

## Quick Start

### 1. Register a project

```bat
ctx.bat project register --repo D:\Work\MyApp --name "My App"
```

### 2. Create a task

```bat
ctx.bat --project D:\Work\MyApp task create "Bootstrap obsmcp" --description "Initialize continuity for this repo"
```

### 3. Mark it current

```bat
ctx.bat --project D:\Work\MyApp start TASK-REPLACE-ME
```

### 4. Run startup safety checks

```bat
ctx.bat --project D:\Work\MyApp preflight --actor codex --initial-request "Continue implementation" --goal "Complete the feature safely"
ctx.bat --project D:\Work\MyApp resume-board
```

### 5. Open a named session

```bat
ctx.bat session open ^
  --actor codex ^
  --client vscode-codex ^
  --model gpt-5 ^
  --project-path D:\Work\MyApp ^
  --task TASK-REPLACE-ME ^
  --label "Managing Director Email" ^
  --workstream managing-director-email ^
  --initial-request "This task is for the managing director's email." ^
  --goal "Draft and finalize the email"
```

### 6. Log work as you go

```bat
ctx.bat --project D:\Work\MyApp log "Drafted the first version" --task TASK-REPLACE-ME --files README.md
```

### 7. Close with a handoff

```bat
ctx.bat handoff --summary "Draft is complete" --next-steps "Review tone and finalize" --to "next-agent"
ctx.bat session close SESSION-REPLACE-ME --actor codex --summary "Closed cleanly with handoff."
```

## MCP Tool Catalog

`obsmcp` currently exposes `117` MCP tools.

This is a deliberately broad surface because `obsmcp` is not only a memory tool. It is a continuity, context, code-understanding, and workflow-management server.

<details>
<summary><strong>Project & Workspace</strong></summary>

- `register_project`: Register a repo with obsmcp and create its centralized workspace.
- `list_projects`: List registered obsmcp projects.
- `resolve_project`: Resolve a project by slug or repo path.
- `resolve_active_project`: Resolve the active project from IDE metadata such as cwd, active file, workspace folders, open files, session_id, task_id, repo_path, or environment hints. Use this before the first continuity write from a plugin or IDE client.
- `get_project_workspace_paths`: Return the workspace paths for a project.
- `attach_repo_bridge`: Write a lightweight bridge file into the repo that points at the centralized obsmcp workspace.
- `migrate_project_layout`: Copy legacy repo-local `.context` and `obsidian/vault` content into the centralized project workspace and attach a repo bridge.
- `sync_hub`: Refresh the central obsmcp hub vault from the registry.
- `health_check`: Return health information about obsmcp.
- `get_server_capabilities`: Return server API/schema versions and supported workflow-safety capabilities.
- `check_client_compatibility`: Compare client API/tool-schema expectations with the current server.
- `list_tools`: Return the obsmcp tool catalog.
- `list_resources`: Return the obsmcp resource catalog.
- `export_project`: Export full project state as JSON (gzipped) and/or Markdown bundle. Creates a timestamped export in `data/exports/`.
- `get_or_create_project`: Auto-detect or create a project from a path hint, session, task, or environment. Resolves from multiple sources and optionally registers if not known. Returns project type metadata, workspace type, and nearby projects.

</details>

<details>
<summary><strong>Project Memory & Notes</strong></summary>

- `get_project_brief`: Return the current project brief sections.
- `get_current_task`: Return the current task.
- `get_active_tasks`: Return open, in-progress, and blocked tasks.
- `get_latest_handoff`: Return the latest handoff.
- `get_recent_work`: Return recent work logs with cursor-style `limit` and `after_id` parameters.
- `get_decisions`: Return recent decisions with cursor-style `limit` and `after_id` parameters.
- `get_blockers`: Return open blockers with cursor-based pagination.
- `get_relevant_files`: Return relevant file paths for a task or the current task.
- `get_table_schema`: Return the SQLite schema for a given table.
- `search_notes`: Search the Obsidian vault for notes.
- `read_note`: Read a note from the Obsidian vault.
- `get_project_status_snapshot`: Return a compact project status snapshot.

</details>

<details>
<summary><strong>Tasks, Decisions & Daily Ops</strong></summary>

- `log_work`: Append a work log entry.
- `log_checkpoint`: Record a completed checkpoint or subtask for a task.
- `update_task`: Update an existing task.
- `create_task`: Create a task.
- `get_task_progress`: Return checkpoint progress and recent checkpoints for a task.
- `log_decision`: Record an ADR-style decision.
- `log_blocker`: Record a blocker.
- `resolve_blocker`: Resolve an open blocker.
- `create_handoff`: Create a model-to-model or user-to-model handoff.
- `append_handoff_note`: Append an additional note to an existing handoff.
- `update_project_brief_section`: Update a named project brief section.
- `create_daily_note_entry`: Append an entry to the daily note stream.
- `set_current_task`: Set the current active task.
- `get_task_templates`: List all available task templates.
- `get_task_template`: Get a specific task template by name.
- `create_task_template`: Create a new task template.
- `delete_task_template`: Delete a task template by name.
- `create_task_from_template`: Create a task from a named template, filling in template variables.
- `quick_log`: One-liner work log that auto-tags the current task. No `task_id` required.
- `get_audit_log`: Full project-wide activity timeline with cursor-based pagination.
- `reset_project`: Wipe project data by scope with audit tracking.
- `bulk_task_ops`: Execute multiple task operations atomically.

</details>

<details>
<summary><strong>Sessions, Startup & Recovery</strong></summary>

- `session_open`: Open an auditable AI session with heartbeat and write-back policy.
- `session_heartbeat`: Record a session heartbeat and optionally emit a heartbeat work log.
- `session_close`: Close a session with summary and optional handoff creation.
- `get_active_sessions`: List open tracked sessions with cursor-based pagination.
- `detect_missing_writeback`: Audit sessions for missing write-back, missing handoffs, or overdue heartbeats.
- `get_startup_preflight`: Run startup safety checks before opening or resuming a session.
- `get_resume_board`: Return a startup dashboard of open tasks, paused tasks, stale sessions, latest handoffs, and the recommended resume target.
- `generate_resume_packet`: Generate a compact resume packet for the next tool or model and write it to the project workspace.
- `generate_emergency_handoff`: Generate a best-effort handoff from persisted state when a session ended abruptly.
- `recover_session`: Recover an interrupted session by generating an emergency handoff and resume packet.
- `session_replay`: Reconstruct the timeline of events within a session.
- `generate_cross_tool_handoff`: Generate a structured JSON handoff payload for another tool or IDE.
- `get_session_lineage_chain`: Traverse parent/child session lineage.
- `set_session_environment`: Attach IDE/environment metadata to an active session.

</details>

<details>
<summary><strong>Context Engineering & Token Efficiency</strong></summary>

- `sync_context_files`: Force a sync of generated context and Obsidian files.
- `generate_compact_context`: Generate compact context for manual prompt injection.
- `generate_compact_context_v2`: Token-budget-aware compact context with decision chains, dependency map, session info, and smart truncation.
- `generate_context_profile`: Generate a cached tiered context profile such as `fast`, `balanced`, `deep`, `handoff`, or `recovery`.
- `generate_delta_context`: Generate a compact delta view showing what changed since a handoff, session, or timestamp.
- `generate_prompt_segments`: Generate stable and dynamic prompt segments for cache-friendly context assembly.
- `generate_retrieval_context`: Generate retrieval-first context with ranked files, recent work, decisions, blockers, and semantic hits for a query.
- `generate_task_snapshot`: Generate a detailed snapshot for a task.
- `record_token_usage`: Record provider or local token usage metrics, including prompt cache fields and compaction savings.
- `get_token_usage_stats`: Return recent token, compaction, and prompt-cache usage aggregates for the project.
- `get_output_response_policy`: Resolve the effective output-token policy for the current task/operation.
- `compact_tool_output`: Compact noisy tool output and optionally save full raw output for debugging.
- `compact_response`: Compress verbose text output while preserving code blocks, URLs, file paths, and errors.
- `get_raw_output_capture`: Retrieve metadata or full content for a saved raw output capture.
- `get_fast_path_response`: Return a deterministic no-LLM fast-path response for common startup and status needs.
- `get_optimization_policy`: Return the active adaptive optimization policy for a mode, task, command, and exit state.
- `list_context_chunks`: List prioritized chunk metadata for a context artifact.
- `generate_progressive_context`: Render one or more prioritized chunks from a context artifact.
- `generate_startup_context`: Generate a delta-first startup context with fast baseline, recent command history, and execution hints.
- `generate_startup_prompt_template`: Return the first-contact startup prompt template for tools and agents.
- `generate_fast_context`: Generate a guaranteed-fast L0-only context for startup/resume use cases.
- `retrieve_context_chunk`: Retrieve a specific chunk of a context artifact for large profile navigation.

</details>

<details>
<summary><strong>Command Intelligence</strong></summary>

- `record_command_event`: Record a terminal command outcome with compact summaries and optional raw output capture.
- `record_command_batch`: Record a batch of command outcomes and return an aggregate summary with risk counts.
- `get_command_event`: Retrieve a recorded command event by ID.
- `get_recent_commands`: List recent recorded command events with cursor-based pagination.
- `get_last_command_result`: Return the most recent recorded command event for a session or task.
- `get_command_failures`: List recent failing command events for a session or task.
- `get_command_execution_policy`: Classify a command for batching and review risk.

</details>

<details>
<summary><strong>Code Atlas & Semantic Knowledge</strong></summary>

- `scan_codebase`: Scan the project directory and generate a Code Atlas documenting every file, function, class, and feature.
- `get_code_atlas_status`: Return current atlas status without regenerating it.
- `start_scan_job`: Queue a background Code Atlas scan job.
- `get_scan_job`: Get the current status and result payload for a background scan job.
- `list_scan_jobs`: List recent background scan jobs for the project.
- `wait_for_scan_job`: Poll a background scan job until it completes or times out.
- `describe_module`: Return a cached or freshly generated semantic description for a module/file.
- `describe_symbol`: Return a semantic description for a function or class.
- `describe_feature`: Return a semantic description for a feature tag from the Code Atlas.
- `search_code_knowledge`: Search semantic knowledge and symbol index entries.
- `get_symbol_candidates`: Return matching function/class symbol candidates for a name.
- `get_related_symbols`: Return nearby or feature-related symbols for a semantic entity.
- `invalidate_semantic_cache`: Mark semantic description cache entries stale by entity or file.
- `refresh_semantic_description`: Force a fresh semantic description generation for an entity lookup.

</details>

<details>
<summary><strong>Dependencies & Retention</strong></summary>

- `configure_log_expiry`: Set the work log retention period in days.
- `expire_old_logs`: Purge work logs older than the configured retention period.
- `get_log_stats`: Return work log statistics and current expiry settings.
- `add_task_dependency`: Link a task as blocked by other tasks and/or blocking other tasks.
- `remove_task_dependency`: Remove task dependencies.
- `get_task_dependency`: Get dependencies for a specific task.
- `get_all_dependencies`: Get all task dependencies across the project.
- `get_blocked_tasks`: Return tasks currently blocked by unresolved dependencies.
- `validate_dependencies`: Validate all task dependencies.

</details>

<details>
<summary><strong>External / Provider Tools</strong></summary>

- `web_search`: Run a web search through obsmcp using the configured provider.
- `understand_image`: Analyze an image through obsmcp using the configured provider.

</details>

## Comparison With Other MCP Servers

This section is intentionally practical and honest.

Not all MCP servers solve the same problem, so this is not a strict "winner takes all" comparison.

`obsmcp` is strongest when you care about continuity, restart safety, project memory, and developer operations.

It is not automatically the best choice when you only need one narrow capability like browser control or GitHub automation.

### Comparison matrix

| Server / category | What it is best at | Where it wins | Where `obsmcp` wins | Where `obsmcp` is weaker |
| --- | --- | --- | --- | --- |
| **Caveman / DIY MCP stack** | Minimal custom setup, hand-rolled memory, quick experiments | Lowest conceptual overhead, easiest to customize quickly | Structured continuity, task/handoff/session management, token-aware startup, auditability, semantic knowledge | `obsmcp` is heavier and more opinionated than a tiny one-file or prompt-only setup |
| **[Context Portal / ConPort](https://github.com/GreatScottyMac/context-portal)** | Project-specific memory bank and RAG backend | Strong structured project memory, SQLite workspace, knowledge graph, semantic search | Stronger session lifecycle, handoffs, startup safety rails, resume board, output/token engineering, command intelligence | ConPort is more narrowly focused on memory-bank workflows and may feel simpler if that is all you need |
| **[Mem0 / OpenMemory MCP](https://github.com/mem0ai/mem0)** | Long-term agent memory and retrieval | Strong memory-centric positioning, retrieval focus, secure/local memory story | Better project operations, richer handoffs, explicit current-task/task dependency model, audit trail, code atlas, session recovery | Mem0 is more specialized if your main goal is reusable memory across many assistants rather than project execution workflow |
| **[Claude-Flow / RuFlow ecosystem](https://github.com/ruvnet/ruflo)** | Multi-agent orchestration and swarm-style automation | Agent orchestration, large tool surface, automation-heavy workflows | Simpler local continuity model, cleaner project-state tracking, more explicit handoffs and restart safety, lower operational sprawl for solo/small-team dev work | `obsmcp` is not a swarm/orchestration platform and does less around multi-agent hive execution |
| **[GitHub MCP Server](https://docs.github.com/en/copilot/how-tos/provide-context/use-mcp/use-the-github-mcp-server)** | GitHub-native repository, issue, PR, and workflow operations | Best when the task is "work with GitHub itself" | Better persistent local continuity, local task/project memory, handoff discipline, codebase restart context | `obsmcp` is not a replacement for deep GitHub API operations |
| **[Playwright MCP](https://github.com/microsoft/playwright-mcp)** | Browser automation, testing, and UI interaction | Best-in-class for browser workflows | Better at long-lived project memory, multi-session continuity, local project governance | `obsmcp` does not replace a browser automation specialist |
| **[Model Context Protocol reference servers](https://github.com/modelcontextprotocol/servers)** | Focused single-purpose tools like filesystem, fetch, git, and memory | Simple, composable, narrow tools with low ambiguity | `obsmcp` unifies continuity, startup context, handoffs, sessions, semantic code understanding, and optimization in one system | The reference servers are usually simpler and easier to reason about when you only need one narrow capability |

### Token-saving comparison

| Server / category | Token-saving approach | Strengths | Limits |
| --- | --- | --- | --- |
| `obsmcp` | Tiered context profiles, delta context, retrieval context, semantic lookups, command-output compaction, output-response policy, token metrics | Broadest token strategy across both input and selected output surfaces | More moving parts to understand and tune |
| ConPort | Structured project memory, queryable context, vector/RAG support, prompt-caching-friendly structure | Good for memory retrieval over large project memory | Less focused on session startup packets, handoff discipline, and output compaction |
| Mem0 | Memory retrieval instead of full-history replay | Strong long-term memory efficiency story | Not a full project continuity and startup-governance layer |
| Claude-Flow / RuFlow | Orchestration, tool specialization, workflow automation | Can reduce manual prompting through agent specialization | More orchestration overhead; not primarily a continuity/token-governance system |
| GitHub MCP | Tool-level context scoping inside GitHub workflows | Prevents over-fetching when the task is GitHub-specific | Does not solve local repo continuity or multi-session task memory |
| Playwright MCP | Tool use instead of verbose browser transcripts | Efficient for UI execution flows | Not a continuity engine |
| DIY / Caveman | Minimal overhead by doing almost nothing automatically | Low system overhead | Most token discipline must be done manually by the operator |

### Feature-by-feature perspective for developers

| Feature | obsmcp | Typical narrow MCP server |
| --- | --- | --- |
| Project-scoped memory | Strong | Usually weak or absent |
| Current task tracking | Native | Usually absent |
| Structured handoffs | Native | Usually absent |
| Resume safety | Strong | Usually manual |
| Session lifecycle | Strong | Often minimal |
| Token-aware startup context | Strong | Often absent |
| Code semantic understanding | Strong | Usually absent unless specialized |
| Browser automation | Weak by itself | Strong in Playwright MCP |
| GitHub automation | Moderate to weak | Strong in GitHub MCP |
| Memory graph / agent memory | Moderate to strong | Strong in memory-specialized servers |
| Operational simplicity | Moderate | Often simpler in narrow servers |
| Auditability | Strong | Varies widely |

### Important honesty note on "Caveman" and "RuFlow"

As of April 14, 2026, I could verify a maintained public ecosystem around `ruvnet/ruflo` / Claude-Flow-style orchestration, but I could not verify one single canonical MCP product named `Caveman` in the same way. In this README, `Caveman` is therefore treated as shorthand for a very minimal, DIY, or hand-rolled MCP + prompt-memory approach rather than a verified official comparison target.

That distinction matters, because `obsmcp` is strongest when compared against:

- DIY continuity systems
- memory-bank-only MCP servers
- orchestration-heavy MCP stacks
- narrow specialist MCP servers

## Where obsmcp Wins

Choose `obsmcp` when you want:

- one continuity layer for many clients
- durable task/session/handoff state
- safer restarts after interruptions
- token-aware startup and resume
- explicit blockers, decisions, and relevant files
- semantic code understanding tied to project continuity
- auditable AI work instead of hidden chat-only memory

It is especially strong for:

- long-lived coding projects
- multi-day AI-assisted development
- model switching and handoffs
- teams experimenting with multiple AI clients
- debugging "the model forgot what it was doing" problems
- controlling token costs on large projects

## Where obsmcp Is Weaker

Choose another tool, or combine another MCP with `obsmcp`, when you need:

- first-class browser automation: use Playwright MCP
- heavy GitHub-native workflows: use GitHub MCP Server
- swarm-style multi-agent orchestration: use Claude-Flow / RuFlow
- a simpler memory-bank-only system: use ConPort or Mem0
- the smallest possible setup with almost zero concepts: use a DIY minimal server

Current practical cons of `obsmcp`:

- Windows-first scripts and docs
- broad tool surface can feel large at first
- more state and moving parts than narrow single-purpose servers
- output-token enforcement only applies where `obsmcp` controls generation
- not a replacement for specialist browser or GitHub automation servers
- not a full multi-agent orchestration framework

## Documentation Index

- [Architecture](docs/ARCHITECTURE.md)
- [Usage Guide](docs/USAGE.md)
- [Installation Guide](docs/INSTALLATION.md)
- [Folder Structure](docs/FOLDER_STRUCTURE.md)
- [Obsidian Integration](docs/OBSIDIAN.md)
- [Startup Automation](docs/STARTUP.md)
- [Testing](docs/TESTING.md)
- [Troubleshooting](docs/TROUBLESHOOTING.md)

## Bottom Line

If you need a **single-purpose MCP server**, there are excellent specialized options.

If you need a **project continuity system for real development work** that can:

- remember what is happening
- tell the next model what matters
- survive interruptions
- reduce token waste
- track tasks and handoffs
- understand the codebase

then `obsmcp` is a much stronger foundation than a basic MCP tool wrapper or a purely chat-memory approach.
