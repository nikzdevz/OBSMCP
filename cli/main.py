from __future__ import annotations

import argparse
import json
from typing import Any

from server.config import load_config
from server.service import ObsmcpService


def _print(value: Any) -> None:
    if isinstance(value, str):
        print(value)
        return
    print(json.dumps(value, indent=2, ensure_ascii=True))


def _csv(value: str | None) -> list[str]:
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ctx", description="obsmcp continuity CLI")
    parser.add_argument("--config", default=None, help="Path to obsmcp config JSON.")
    parser.add_argument("--project", dest="project_path", default=None, help="Project root path. Defaults to OBSMCP_PROJECT env var or configured default.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    start = subparsers.add_parser("start", help="Set the current task and mark it in progress.")
    start.add_argument("task_id")
    start.add_argument("--actor", default="ctx")

    log = subparsers.add_parser("log", help="Append a work log entry.")
    log.add_argument("message")
    log.add_argument("--task", dest="task_id")
    log.add_argument("--summary")
    log.add_argument("--files")
    log.add_argument("--actor", default="ctx")

    handoff = subparsers.add_parser("handoff", help="Create a handoff for another model or tool.")
    handoff.add_argument("--summary", required=True)
    handoff.add_argument("--next-steps", default="")
    handoff.add_argument("--open-questions", default="")
    handoff.add_argument("--note", default="")
    handoff.add_argument("--task", dest="task_id")
    handoff.add_argument("--from", dest="from_actor", default="ctx")
    handoff.add_argument("--to", dest="to_actor", default="next-agent")

    sync = subparsers.add_parser("sync", help="Regenerate .context and Obsidian files.")

    status = subparsers.add_parser("status", help="Show the project status snapshot.")
    preflight = subparsers.add_parser("preflight", help="Run startup safety checks before opening or resuming a session.")
    preflight.add_argument("--actor", default="")
    preflight.add_argument("--task", dest="task_id")
    preflight.add_argument("--session", dest="session_id")
    preflight.add_argument("--initial-request", default="")
    preflight.add_argument("--goal", dest="session_goal", default="")
    preflight.add_argument("--label", dest="session_label", default="")
    preflight.add_argument("--workstream", dest="workstream_key", default="")
    preflight.add_argument("--client", dest="client_name", default="")
    preflight.add_argument("--model", dest="model_name", default="")
    resume_board = subparsers.add_parser("resume-board", help="Show the startup resume dashboard.")
    compat = subparsers.add_parser("compat", help="Check client/server compatibility.")
    compat.add_argument("--client-api-version", default="")
    compat.add_argument("--client-tool-schema-version", type=int)
    compat.add_argument("--client", dest="client_name", default="")
    compat.add_argument("--model", dest="model_name", default="")

    blockers = subparsers.add_parser("blockers", help="Show open blockers.")

    note = subparsers.add_parser("note", help="Append a daily note entry.")
    note.add_argument("entry")
    note.add_argument("--date", dest="note_date")
    note.add_argument("--actor", default="ctx")

    current = subparsers.add_parser("current", help="Show the current task.")

    project = subparsers.add_parser("project", help="Project registration and workspace utilities.")
    project_sub = project.add_subparsers(dest="project_command", required=True)
    project_register = project_sub.add_parser("register", help="Register a repo with obsmcp.")
    project_register.add_argument("--repo", dest="repo_path", required=True)
    project_register.add_argument("--name")
    project_register.add_argument("--tags")
    project_list = project_sub.add_parser("list", help="List registered projects.")
    project_paths = project_sub.add_parser("paths", help="Show workspace paths for a project.")
    project_paths.add_argument("--slug", dest="project_slug")
    project_paths.add_argument("--repo", dest="project_repo")
    project_migrate = project_sub.add_parser("migrate", help="Migrate legacy repo-local obsmcp notes/context into the centralized workspace.")
    project_migrate.add_argument("--slug", dest="project_slug")
    project_migrate.add_argument("--repo", dest="project_repo")

    session = subparsers.add_parser("session", help="Session operations.")
    session_sub = session.add_subparsers(dest="session_command", required=True)

    session_open = session_sub.add_parser("open", help="Open a tracked AI work session.")
    session_open.add_argument("--actor", required=True)
    session_open.add_argument("--client", dest="client_name", default="")
    session_open.add_argument("--model", dest="model_name", default="")
    session_open.add_argument("--label", dest="session_label", default="")
    session_open.add_argument("--workstream", dest="workstream_key", default="")
    session_open.add_argument("--workstream-title", dest="workstream_title", default="")
    session_open.add_argument("--project-path", dest="session_project_path", default=None)
    session_open.add_argument("--initial-request", default="")
    session_open.add_argument("--goal", dest="session_goal", default="")
    session_open.add_argument("--task", dest="task_id")
    session_open.add_argument("--resume-strategy", choices=["auto", "new", "resume"], default="auto")
    session_open.add_argument("--resume-session-id", default=None)
    session_open.add_argument("--heartbeat-seconds", dest="heartbeat_interval_seconds", type=int, default=900)
    session_open.add_argument("--worklog-seconds", dest="work_log_interval_seconds", type=int, default=1800)
    session_open.add_argument("--min-worklogs", dest="min_work_logs", type=int, default=1)

    session_heartbeat = session_sub.add_parser("heartbeat", help="Heartbeat an active session.")
    session_heartbeat.add_argument("session_id")
    session_heartbeat.add_argument("--actor", required=True)
    session_heartbeat.add_argument("--note", dest="status_note", default="")
    session_heartbeat.add_argument("--task", dest="task_id")
    session_heartbeat.add_argument("--files")
    session_heartbeat.add_argument("--create-work-log", action="store_true")

    session_close = session_sub.add_parser("close", help="Close a session and optionally create a handoff.")
    session_close.add_argument("session_id")
    session_close.add_argument("--actor", required=True)
    session_close.add_argument("--summary", default="")
    session_close.add_argument("--handoff-summary", default="")
    session_close.add_argument("--handoff-next-steps", default="")
    session_close.add_argument("--handoff-open-questions", default="")
    session_close.add_argument("--handoff-note", default="")
    session_close.add_argument("--handoff-to", dest="handoff_to_actor", default="next-agent")
    session_close.add_argument("--skip-handoff", action="store_true")

    session_list = session_sub.add_parser("list", help="List active sessions.")

    atlas = subparsers.add_parser("atlas", help="Code Atlas — scan and document the entire codebase.")
    atlas.add_argument("action", nargs="?", default="status", choices=["status", "refresh", "generate", "jobs", "job", "wait"], help="'status' = check atlas state. 'refresh' = regenerate if stale. 'generate' = force regenerate. 'jobs'/'job'/'wait' operate on background scan jobs.")
    atlas.add_argument("job_id", nargs="?", default=None, help="Optional scan job ID for 'job' or 'wait'.")
    atlas.add_argument("--force", action="store_true", help="Force full regeneration even if up to date.")
    atlas.add_argument("--background", action="store_true", help="Queue the scan in the background and return a pollable job.")
    atlas.add_argument("--wait", action="store_true", help="After queueing a background scan, wait for completion.")
    atlas.add_argument("--wait-seconds", type=int, default=30, help="How long to wait when using --wait or atlas wait.")
    atlas.add_argument("--requested-by", default="ctx", help="Actor label to store on a queued background scan job.")
    atlas.add_argument("--status", dest="job_status", choices=["queued", "running", "completed", "failed", "interrupted"], help="Filter atlas jobs by status.")

    describe = subparsers.add_parser("describe", help="Semantic knowledge lookups.")
    describe_sub = describe.add_subparsers(dest="describe_command", required=True)
    describe_module = describe_sub.add_parser("module", help="Describe a module/file.")
    describe_module.add_argument("module_path")
    describe_symbol = describe_sub.add_parser("symbol", help="Describe a function or class.")
    describe_symbol.add_argument("symbol_name")
    describe_symbol.add_argument("--module")
    describe_symbol.add_argument("--entity-key")
    describe_symbol.add_argument("--type", dest="entity_type", choices=["function", "class"])
    describe_feature = describe_sub.add_parser("feature", help="Describe a feature tag.")
    describe_feature.add_argument("feature_name")

    knowledge = subparsers.add_parser("knowledge", help="Semantic knowledge search and maintenance.")
    knowledge_sub = knowledge.add_subparsers(dest="knowledge_command", required=True)
    knowledge_search = knowledge_sub.add_parser("search", help="Search semantic knowledge.")
    knowledge_search.add_argument("query")
    knowledge_search.add_argument("--limit", type=int, default=10)
    knowledge_candidates = knowledge_sub.add_parser("candidates", help="Get symbol candidates for a name.")
    knowledge_candidates.add_argument("symbol_name")
    knowledge_candidates.add_argument("--module")
    knowledge_candidates.add_argument("--type", dest="entity_type", choices=["function", "class"])
    knowledge_candidates.add_argument("--limit", type=int, default=20)
    knowledge_related = knowledge_sub.add_parser("related", help="Get related symbols for an entity.")
    knowledge_related.add_argument("entity_key")
    knowledge_related.add_argument("--limit", type=int, default=8)
    knowledge_invalidate = knowledge_sub.add_parser("invalidate", help="Invalidate semantic cache by entity or file.")
    knowledge_invalidate.add_argument("--entity-key")
    knowledge_invalidate.add_argument("--files")
    knowledge_refresh = knowledge_sub.add_parser("refresh", help="Force refresh a semantic description.")
    knowledge_refresh.add_argument("--entity-key")
    knowledge_refresh.add_argument("--module")
    knowledge_refresh.add_argument("--symbol")
    knowledge_refresh.add_argument("--feature")
    knowledge_refresh.add_argument("--type", dest="entity_type", choices=["function", "class"])

    # Phase 4: compact_context_v2 CLI
    compact = subparsers.add_parser("compact", help="Generate compact context v2 with token budget.")
    compact.add_argument("--task", dest="task_id")
    compact.add_argument("--profile", choices=["fast", "balanced", "deep", "handoff", "recovery"], default="deep")
    compact.add_argument("--max-tokens", dest="max_tokens", type=int, default=3000)
    compact.add_argument("--no-decision-chain", dest="include_decision_chain", action="store_false", default=True)
    compact.add_argument("--no-dependency-map", dest="include_dependency_map", action="store_false", default=True)
    compact.add_argument("--no-session-info", dest="include_session_info", action="store_false", default=True)
    compact.add_argument("--no-recent-work", dest="include_recent_work", action="store_false", default=True)
    compact.add_argument("--daily-notes", action="store_true", default=False)

    delta = subparsers.add_parser("delta", help="Generate delta context since a handoff, session, or timestamp.")
    delta.add_argument("--task", dest="task_id")
    delta.add_argument("--handoff", dest="since_handoff_id", type=int)
    delta.add_argument("--session", dest="since_session_id")
    delta.add_argument("--since", dest="since_timestamp")

    audit = subparsers.add_parser("audit", help="Audit sessions for missing write-back and handoffs.")
    audit.add_argument("--include-closed", action="store_true")

    fast = subparsers.add_parser("fast", help="Generate a lightweight L0-only fast context for startup/resume.")
    fast.add_argument("--task", dest="task_id")
    fast.add_argument("--tokens", action="store_true", help="Print token count after output.")

    resume = subparsers.add_parser("resume", help="Generate a resume packet for the active project/session.")
    resume.add_argument("--session", dest="session_id")
    resume.add_argument("--task", dest="task_id")

    recover = subparsers.add_parser("recover", help="Recover an interrupted session with an emergency handoff.")
    recover.add_argument("--session", dest="session_id")
    recover.add_argument("--actor", default="ctx-recovery")

    workspace = subparsers.add_parser("workspace", help="Workspace helper commands.")
    workspace_sub = workspace.add_subparsers(dest="workspace_command", required=True)
    workspace_paths = workspace_sub.add_parser("paths", help="Show the workspace paths for the active project.")
    workspace_paths.add_argument("--slug", dest="project_slug")
    workspace_paths.add_argument("--repo", dest="workspace_repo")

    hub = subparsers.add_parser("hub", help="Central hub utilities.")
    hub_sub = hub.add_subparsers(dest="hub_command", required=True)
    hub_sync = hub_sub.add_parser("sync", help="Refresh the obsmcp hub vault.")

    # Phase 1: Task Templates CLI
    template = subparsers.add_parser("template", help="Task template operations.")
    template_sub = template.add_subparsers(dest="template_command", required=True)
    template_list = template_sub.add_parser("list", help="List all task templates.")
    template_get = template_sub.add_parser("get", help="Get a specific template.")
    template_get.add_argument("name")
    template_create = template_sub.add_parser("create", help="Create a new task template.")
    template_create.add_argument("name")
    template_create.add_argument("--title", required=True, help="Title template with {placeholders}")
    template_create.add_argument("--description", required=True, help="Description template with {placeholders}")
    template_create.add_argument("--priority", default="medium")
    template_create.add_argument("--tags")
    template_delete = template_sub.add_parser("delete", help="Delete a task template.")
    template_delete.add_argument("name")

    # Phase 1: Quick Log CLI
    quick = subparsers.add_parser("quick", help="Quick work log — auto-tags current task.")
    quick.add_argument("message")
    quick.add_argument("--files")
    quick.add_argument("--actor", default="ctx")

    # Phase 1: Audit Log CLI
    audit_log = subparsers.add_parser("audit-log", help="Full activity timeline.")
    audit_log.add_argument("--actor")
    audit_log.add_argument("--task")
    audit_log.add_argument("--type")
    audit_log.add_argument("--from")
    audit_log.add_argument("--to")
    audit_log.add_argument("--limit", type=int, default=100)
    audit_log.add_argument("--ai-only", action="store_true")

    # Phase 2: Reset Project CLI
    reset = subparsers.add_parser("reset", help="Reset project data by scope. WARNING: permanently deletes data.")
    reset.add_argument("--scope", required=True, choices=["tasks", "blockers", "sessions", "work_logs", "decisions", "handoffs", "full"], help="Scope to reset")
    reset.add_argument("--actor", default="ctx")

    # Phase 2: Bulk Task Ops CLI
    bulk = subparsers.add_parser("bulk", help="Bulk task operations (JSON array of operations).")
    bulk.add_argument("operations", help='JSON array, e.g. \'[{"action":"create","title":"X","description":"Y"}]\'')

    # Phase 2: Project Export CLI
    export = subparsers.add_parser("export", help="Export project state.")
    export.add_argument("--format", default="both", choices=["json", "markdown", "both"])

    # Phase 3: Work Log Expiry CLI
    logs = subparsers.add_parser("logs", help="Work log operations.")
    logs_sub = logs.add_subparsers(dest="logs_command", required=True)
    logs_stats = logs_sub.add_parser("stats", help="Show log statistics by age.")
    logs_expire = logs_sub.add_parser("expire", help="Purge old logs.")
    logs_expire.add_argument("--days", type=int, help="Override retention days (default: use configured)")
    logs_expire.add_argument("--actor", default="ctx")
    logs_config = logs_sub.add_parser("config", help="Configure log expiry.")
    logs_config.add_argument("days", type=int, help="Retention days (0=disable)")

    # Phase 3: Session Replay CLI
    session_replay = subparsers.add_parser("replay", help="Replay a session timeline.")
    session_replay.add_argument("session_id", nargs="?", help="Session ID (defaults to most recent)")

    # Phase 3: Task Dependency CLI
    deps = subparsers.add_parser("deps", help="Task dependency operations.")
    deps_sub = deps.add_subparsers(dest="deps_command", required=True)
    deps_add = deps_sub.add_parser("add", help="Add dependency.")
    deps_add.add_argument("task_id")
    deps_add.add_argument("--blocked-by", help="Comma-separated task IDs this is blocked by")
    deps_add.add_argument("--blocks", help="Comma-separated task IDs this blocks")
    deps_remove = deps_sub.add_parser("remove", help="Remove dependency.")
    deps_remove.add_argument("task_id")
    deps_remove.add_argument("--blocked-by", help="Comma-separated task IDs to unlink")
    deps_remove.add_argument("--blocks", help="Comma-separated task IDs to unlink")
    deps_list = deps_sub.add_parser("list", help="List all dependencies.")
    deps_blocked = deps_sub.add_parser("blocked", help="List blocked tasks.")
    deps_validate = deps_sub.add_parser("validate", help="Validate all dependencies.")

    task = subparsers.add_parser("task", help="Task operations.")
    task_sub = task.add_subparsers(dest="task_command", required=True)

    task_create = task_sub.add_parser("create", help="Create a new task.")
    task_create.add_argument("title")
    task_create.add_argument("--description", required=True)
    task_create.add_argument("--priority", default="medium")
    task_create.add_argument("--owner")
    task_create.add_argument("--files")
    task_create.add_argument("--tags")
    task_create.add_argument("--actor", default="ctx")

    task_update = task_sub.add_parser("update", help="Update an existing task.")
    task_update.add_argument("task_id")
    task_update.add_argument("--title")
    task_update.add_argument("--description")
    task_update.add_argument("--status")
    task_update.add_argument("--priority")
    task_update.add_argument("--owner")
    task_update.add_argument("--files")
    task_update.add_argument("--tags")
    task_update.add_argument("--actor", default="ctx")

    decision = subparsers.add_parser("decision", help="Decision operations.")
    decision_sub = decision.add_subparsers(dest="decision_command", required=True)

    decision_log = decision_sub.add_parser("log", help="Record a decision.")
    decision_log.add_argument("title")
    decision_log.add_argument("--decision", required=True)
    decision_log.add_argument("--rationale", default="")
    decision_log.add_argument("--impact", default="")
    decision_log.add_argument("--task", dest="task_id")
    decision_log.add_argument("--actor", default="ctx")

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    config = load_config(args.config)
    service = ObsmcpService(config)
    project_path = args.project_path

    if args.command == "start":
        _print(service.set_current_task(task_id=args.task_id, actor=args.actor, project_path=project_path))
        return

    if args.command == "log":
        _print(
            service.log_work(
                message=args.message,
                task_id=args.task_id,
                summary=args.summary,
                files=_csv(args.files),
                actor=args.actor,
                project_path=project_path,
            )
        )
        return

    if args.command == "handoff":
        task_id = args.task_id or (service.get_current_task(project_path=project_path) or {}).get("id")
        _print(
            service.create_handoff(
                summary=args.summary,
                next_steps=args.next_steps,
                open_questions=args.open_questions,
                note=args.note,
                task_id=task_id,
                from_actor=args.from_actor,
                to_actor=args.to_actor,
                project_path=project_path,
            )
        )
        return

    if args.command == "sync":
        _print(service.sync_context(project_path=project_path))
        return

    if args.command == "fast":
        result = service.generate_fast_context(task_id=args.task_id, project_path=project_path)
        if args.tokens:
            print(result["markdown"], end="")
            print(f"\n--- {result['used_tokens']} tokens ---")
        else:
            _print(result)
        return

    if args.command == "status":
        _print(service.get_project_status_snapshot(project_path=project_path))
        return

    if args.command == "preflight":
        _print(
            service.get_startup_preflight(
                actor=args.actor,
                task_id=args.task_id,
                session_id=args.session_id,
                initial_request=args.initial_request,
                session_goal=args.session_goal,
                session_label=args.session_label,
                workstream_key=args.workstream_key,
                client_name=args.client_name,
                model_name=args.model_name,
                project_path=project_path,
            )
        )
        return

    if args.command == "resume-board":
        _print(service.get_resume_board(project_path=project_path))
        return

    if args.command == "compat":
        _print(
            service.check_client_compatibility(
                client_api_version=args.client_api_version,
                client_tool_schema_version=args.client_tool_schema_version,
                client_name=args.client_name,
                model_name=args.model_name,
                project_path=project_path,
            )
        )
        return

    if args.command == "blockers":
        _print(service.get_blockers(project_path=project_path))
        return

    if args.command == "note":
        _print(service.create_daily_note_entry(entry=args.entry, actor=args.actor, note_date=args.note_date, project_path=project_path))
        return

    if args.command == "current":
        _print(service.get_current_task(project_path=project_path) or {"message": "No current task set."})
        return

    if args.command == "project":
        if args.project_command == "register":
            _print(service.register_project(repo_path=args.repo_path, name=args.name, tags=_csv(args.tags)))
            return
        if args.project_command == "list":
            _print(service.list_projects())
            return
        if args.project_command == "paths":
            _print(service.get_project_workspace_paths(project_slug=args.project_slug, project_path=args.project_repo or project_path))
            return
        if args.project_command == "migrate":
            _print(service.migrate_project_layout(project_slug=args.project_slug, project_path=args.project_repo or project_path))
            return

    if args.command == "session":
        if args.session_command == "open":
            effective_project_path = args.session_project_path or project_path
            _print(
                service.session_open(
                    actor=args.actor,
                    client_name=args.client_name,
                    model_name=args.model_name,
                    session_label=args.session_label,
                    workstream_key=args.workstream_key,
                    workstream_title=args.workstream_title,
                    project_path=effective_project_path,
                    initial_request=args.initial_request,
                    session_goal=args.session_goal,
                    task_id=args.task_id,
                    heartbeat_interval_seconds=args.heartbeat_interval_seconds,
                    work_log_interval_seconds=args.work_log_interval_seconds,
                    min_work_logs=args.min_work_logs,
                    resume_strategy=args.resume_strategy,
                    resume_session_id=args.resume_session_id,
                )
            )
            return
        if args.session_command == "heartbeat":
            _print(
                service.session_heartbeat(
                    session_id=args.session_id,
                    actor=args.actor,
                    status_note=args.status_note,
                    task_id=args.task_id,
                    files=_csv(args.files),
                    create_work_log=args.create_work_log,
                    project_path=project_path,
                )
            )
            return
        if args.session_command == "close":
            _print(
                service.session_close(
                    session_id=args.session_id,
                    actor=args.actor,
                    summary=args.summary,
                    create_handoff=not args.skip_handoff,
                    handoff_summary=args.handoff_summary,
                    handoff_next_steps=args.handoff_next_steps,
                    handoff_open_questions=args.handoff_open_questions,
                    handoff_note=args.handoff_note,
                    handoff_to_actor=args.handoff_to_actor,
                    project_path=project_path,
                )
            )
            return
        if args.session_command == "list":
            _print(service.get_active_sessions(project_path=project_path))
            return

    if args.command == "atlas":
        if args.action in {"status", None}:
            _print(service.get_code_atlas_status(project_path=project_path))
            return
        if args.action == "jobs":
            _print(service.list_scan_jobs(project_path=project_path, status=args.job_status))
            return
        if args.action == "job":
            if not args.job_id:
                raise SystemExit("atlas job requires JOB_ID")
            _print(service.get_scan_job(args.job_id, project_path=project_path))
            return
        if args.action == "wait":
            if not args.job_id:
                raise SystemExit("atlas wait requires JOB_ID")
            _print(service.wait_for_scan_job(args.job_id, project_path=project_path, wait_seconds=args.wait_seconds))
            return
        force = args.action == "generate" or args.force
        if args.background:
            job = service.start_scan_job(project_path=project_path, force_refresh=force, requested_by=args.requested_by)
            if args.wait:
                _print(service.wait_for_scan_job(job["id"], project_path=project_path, wait_seconds=args.wait_seconds))
            else:
                _print(job)
            return
        _print(service.scan_codebase(force_refresh=force, project_path=project_path))
        return

    if args.command == "describe":
        if args.describe_command == "module":
            _print(service.describe_module(module_path=args.module_path, project_path=project_path))
            return
        if args.describe_command == "symbol":
            _print(
                service.describe_symbol(
                    symbol_name=args.symbol_name,
                    module_path=args.module,
                    entity_key=args.entity_key,
                    entity_type=args.entity_type,
                    project_path=project_path,
                )
            )
            return
        if args.describe_command == "feature":
            _print(service.describe_feature(feature_name=args.feature_name, project_path=project_path))
            return

    if args.command == "knowledge":
        if args.knowledge_command == "search":
            _print(service.search_code_knowledge(query=args.query, limit=args.limit, project_path=project_path))
            return
        if args.knowledge_command == "candidates":
            _print(
                service.get_symbol_candidates(
                    symbol_name=args.symbol_name,
                    module_path=args.module,
                    entity_type=args.entity_type,
                    limit=args.limit,
                    project_path=project_path,
                )
            )
            return
        if args.knowledge_command == "related":
            _print(service.get_related_symbols(entity_key=args.entity_key, limit=args.limit, project_path=project_path))
            return
        if args.knowledge_command == "invalidate":
            _print(
                service.invalidate_semantic_cache(
                    entity_key=args.entity_key,
                    file_paths=_csv(args.files),
                    project_path=project_path,
                )
            )
            return
        if args.knowledge_command == "refresh":
            _print(
                service.refresh_semantic_description(
                    entity_key=args.entity_key,
                    module_path=args.module,
                    symbol_name=args.symbol,
                    feature_name=args.feature,
                    entity_type=args.entity_type,
                    project_path=project_path,
                )
            )
            return

    if args.command == "compact":
        if args.profile == "deep":
            result = service.generate_compact_context_v2(
                task_id=args.task_id,
                max_tokens=args.max_tokens,
                include_decision_chain=args.include_decision_chain,
                include_dependency_map=args.include_dependency_map,
                include_session_info=args.include_session_info,
                include_recent_work=args.include_recent_work,
                include_daily_notes=args.daily_notes,
                project_path=project_path,
            )
        else:
            result = service.generate_context_profile(
                profile=args.profile,
                task_id=args.task_id,
                max_tokens=args.max_tokens,
                include_daily_notes=args.daily_notes,
                project_path=project_path,
            )["markdown"]
        _print(result)
        return

    if args.command == "delta":
        _print(
            service.generate_delta_context(
                task_id=args.task_id,
                since_handoff_id=args.since_handoff_id,
                since_session_id=args.since_session_id,
                since_timestamp=args.since_timestamp,
                project_path=project_path,
            )
        )
        return

    if args.command == "audit":
        _print(service.detect_missing_writeback(include_closed=args.include_closed, project_path=project_path))
        return

    if args.command == "resume":
        _print(service.generate_resume_packet(session_id=args.session_id, task_id=args.task_id, project_path=project_path))
        return

    if args.command == "recover":
        _print(service.recover_session(session_id=args.session_id, actor=args.actor, project_path=project_path))
        return

    if args.command == "workspace":
        if args.workspace_command == "paths":
            _print(service.get_project_workspace_paths(project_slug=args.project_slug, project_path=args.workspace_repo or project_path))
            return

    if args.command == "hub":
        if args.hub_command == "sync":
            _print(service.sync_hub())
            return

    if args.command == "task":
        if args.task_command == "create":
            _print(
                service.create_task(
                    title=args.title,
                    description=args.description,
                    priority=args.priority,
                    owner=args.owner,
                    relevant_files=_csv(args.files),
                    tags=_csv(args.tags),
                    actor=args.actor,
                    project_path=project_path,
                )
            )
            return
        if args.task_command == "update":
            _print(
                service.update_task(
                    task_id=args.task_id,
                    title=args.title,
                    description=args.description,
                    status=args.status,
                    priority=args.priority,
                    owner=args.owner,
                    relevant_files=_csv(args.files) if args.files is not None else None,
                    tags=_csv(args.tags) if args.tags is not None else None,
                    actor=args.actor,
                    project_path=project_path,
                )
            )
            return

    if args.command == "decision":
        if args.decision_command == "log":
            _print(
                service.log_decision(
                    title=args.title,
                    decision=args.decision,
                    rationale=args.rationale,
                    impact=args.impact,
                    task_id=args.task_id,
                    actor=args.actor,
                    project_path=project_path,
                )
            )
            return

    # Phase 1: Template commands
    if args.command == "template":
        store = service._store(project_path)
        if args.template_command == "list":
            _print(store.get_task_templates())
            return
        if args.template_command == "get":
            result = store.get_task_template(args.name)
            _print(result or {"error": f"Template '{args.name}' not found."})
            return
        if args.template_command == "create":
            _print(
                store.create_task_template(
                    name=args.name,
                    title_template=args.title,
                    description_template=args.description,
                    priority=args.priority,
                    tags=_csv(args.tags),
                )
            )
            return
        if args.template_command == "delete":
            deleted = store.delete_task_template(args.name)
            _print({"deleted": deleted, "template": args.name})
            return

    # Phase 1: Quick log
    if args.command == "quick":
        _print(service.quick_log(message=args.message, files=_csv(args.files), actor=args.actor, project_path=project_path))
        return

    # Phase 1: Audit log
    if args.command == "audit-log":
        _print(
            service.get_audit_log(
                actor=args.actor,
                task_id=args.task,
                action_type=args.type,
                from_date=getattr(args, "from"),
                to_date=getattr(args, "to"),
                limit=args.limit,
                include_ai_only=args.ai_only,
                project_path=project_path,
            )
        )
        return

    # Phase 2: Reset project
    if args.command == "reset":
        _print(service.reset_project(scope=args.scope, actor=args.actor, project_path=project_path))
        return

    # Phase 2: Bulk task operations
    if args.command == "bulk":
        import json as _json

        ops = _json.loads(args.operations)
        _print(service.bulk_task_ops(operations=ops, project_path=project_path))
        return

    # Phase 2: Project export
    if args.command == "export":
        _print(service.export_project(format=args.format, project_path=project_path))
        return

    # Phase 3: Work log expiry
    if args.command == "logs":
        if args.logs_command == "stats":
            _print(service.get_log_stats(project_path=project_path))
            return
        if args.logs_command == "expire":
            if args.days is not None:
                service.configure_log_expiry(days=args.days, actor=args.actor, project_path=project_path)
            _print(service.expire_old_logs(actor=args.actor, project_path=project_path))
            return
        if args.logs_command == "config":
            _print(service.configure_log_expiry(days=args.days, actor="ctx", project_path=project_path))
            return

    # Phase 3: Session replay
    if args.command == "replay":
        _print(service.session_replay(session_id=args.session_id, project_path=project_path))
        return

    # Phase 3: Task dependencies
    if args.command == "deps":
        if args.deps_command == "add":
            blocked = [x.strip() for x in (args.blocked_by or "").split(",") if x.strip()]
            blocks = [x.strip() for x in (args.blocks or "").split(",") if x.strip()]
            _print(service.add_task_dependency(task_id=args.task_id, blocked_by=blocked, blocks=blocks, project_path=project_path))
            return
        if args.deps_command == "remove":
            blocked = [x.strip() for x in (args.blocked_by or "").split(",") if x.strip()]
            blocks = [x.strip() for x in (args.blocks or "").split(",") if x.strip()]
            _print(service.remove_task_dependency(task_id=args.task_id, blocked_by=blocked if blocked else None, blocks=blocks if blocks else None, project_path=project_path))
            return
        if args.deps_command == "list":
            _print(service.get_all_dependencies(project_path=project_path))
            return
        if args.deps_command == "blocked":
            _print(service.get_blocked_tasks(project_path=project_path))
            return
        if args.deps_command == "validate":
            _print(service.validate_dependencies(project_path=project_path))
            return

    parser.error("Unknown command")


if __name__ == "__main__":
    main()
