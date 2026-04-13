from __future__ import annotations

from typing import Any

from .config import AppConfig, ProjectConfig
from .store import StateStore
from .utils import write_json_atomic, write_text_atomic


def render_project_context(store: StateStore) -> str:
    brief = store.get_project_brief()
    current_task = store.get_current_task()
    recent_work = store.get_recent_work(limit=6)
    blockers = store.get_blockers(open_only=True, limit=6)
    latest_handoff = store.get_latest_handoff()
    semantic_modules = []
    if current_task:
        for file_path in store.get_relevant_files(current_task["id"], limit=8):
            module = store.get_module_index(file_path)
            if module:
                semantic_modules.append(module)

    lines = [
        "# PROJECT CONTEXT",
        "",
        "This file is machine-generated from the obsmcp structured state.",
        "",
    ]
    for section, content in brief.items():
        lines.extend([f"## {section}", "", content.strip() or "No content yet.", ""])

    lines.extend(["## Current Task", ""])
    if current_task:
        lines.extend(
            [
                f"- ID: {current_task['id']}",
                f"- Title: {current_task['title']}",
                f"- Status: {current_task['status']}",
                f"- Priority: {current_task['priority']}",
                "",
                current_task["description"],
                "",
            ]
        )
    else:
        lines.extend(["No active current task is set.", ""])

    lines.extend(["## Open Blockers", ""])
    if blockers:
        for blocker in blockers:
            lines.append(f"- [{blocker['id']}] {blocker['title']}: {blocker['description']}")
    else:
        lines.append("- None")
    lines.append("")

    lines.extend(["## Recent Work", ""])
    if recent_work:
        for item in recent_work:
            task_label = item["task_id"] or "no-task"
            lines.append(f"- {item['created_at']} [{task_label}] {item['message']}")
    else:
        lines.append("- No work logs yet.")
    lines.append("")

    lines.extend(["## Recommended Semantic Lookups", ""])
    if semantic_modules:
        seen = set()
        for item in semantic_modules:
            if item["entity_key"] in seen:
                continue
            seen.add(item["entity_key"])
            lines.append(f"- {item['entity_key']}: {item.get('summary_hint', item['name'])}")
    else:
        lines.append("- No semantic lookups recorded yet.")
    lines.append("")

    lines.extend(["## Latest Handoff", ""])
    if latest_handoff:
        lines.extend(
            [
                f"- From: {latest_handoff['from_actor']}",
                f"- To: {latest_handoff['to_actor']}",
                f"- Created: {latest_handoff['created_at']}",
                "",
                latest_handoff["summary"],
                "",
            ]
        )
    else:
        lines.append("No handoff recorded yet.")
        lines.append("")
    return "\n".join(lines).strip() + "\n"


def render_handoff_markdown(handoff: dict[str, Any] | None) -> str:
    if not handoff:
        return "# HANDOFF\n\nNo handoff recorded yet.\n"
    lines = [
        "# HANDOFF",
        "",
        f"- ID: {handoff['id']}",
        f"- Task: {handoff['task_id'] or 'unassigned'}",
        f"- From: {handoff['from_actor']}",
        f"- To: {handoff['to_actor']}",
        f"- Created: {handoff['created_at']}",
        "",
        "## Summary",
        "",
        handoff["summary"] or "No summary provided.",
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
        handoff["note"] or "No extra note.",
        "",
    ]
    return "\n".join(lines)


def render_decisions_markdown(decisions: list[dict[str, Any]]) -> str:
    lines = ["# DECISIONS", ""]
    if not decisions:
        lines.extend(["No decisions recorded yet.", ""])
        return "\n".join(lines)
    for item in decisions:
        lines.extend(
            [
                f"## [{item['id']}] {item['title']}",
                "",
                f"- Task: {item['task_id'] or 'unassigned'}",
                f"- Actor: {item['actor']}",
                f"- Created: {item['created_at']}",
                "",
                item["decision"],
                "",
                f"Rationale: {item['rationale'] or 'Not recorded.'}",
                "",
                f"Impact: {item['impact'] or 'Not recorded.'}",
                "",
            ]
        )
    return "\n".join(lines)


def render_session_summary(store: StateStore) -> str:
    summary = store.get_latest_session_summary()
    if summary:
        return (
            "# SESSION SUMMARY\n\n"
            f"- Label: {summary['session_label']}\n"
            f"- Actor: {summary['actor']}\n"
            f"- Created: {summary['created_at']}\n\n"
            f"{summary['summary']}\n"
        )

    recent_work = store.get_recent_work(limit=5)
    lines = ["# SESSION SUMMARY", "", "No explicit session summary recorded yet.", "", "## Recent Work", ""]
    if recent_work:
        for item in recent_work:
            lines.append(f"- {item['created_at']}: {item['message']}")
    else:
        lines.append("- No work logged yet.")
    lines.append("")
    return "\n".join(lines)


def sync_context_files(config: AppConfig, store: StateStore, project_config: ProjectConfig) -> dict[str, str]:
    current_task = store.get_current_task()
    latest_handoff = store.get_latest_handoff()
    decisions = store.get_decisions(limit=config.max_decisions)
    blockers = store.get_blockers(open_only=True, limit=config.max_blockers)
    relevant_files = store.get_relevant_files(current_task["id"] if current_task else None)
    snapshot = store.get_project_status_snapshot()
    session_audit = store.detect_missing_writeback()
    active_sessions = store.get_active_sessions(limit=50)

    files_written: dict[str, str] = {}
    context_dir = project_config.context_path
    json_export = project_config.json_export_dir
    json_export.mkdir(parents=True, exist_ok=True)

    project_context_path = context_dir / "PROJECT_CONTEXT.md"
    write_text_atomic(project_context_path, render_project_context(store))
    files_written["PROJECT_CONTEXT.md"] = str(project_context_path)

    current_task_path = context_dir / "CURRENT_TASK.json"
    write_json_atomic(current_task_path, current_task or {})
    files_written["CURRENT_TASK.json"] = str(current_task_path)

    handoff_path = context_dir / "HANDOFF.md"
    write_text_atomic(handoff_path, render_handoff_markdown(latest_handoff))
    files_written["HANDOFF.md"] = str(handoff_path)

    decisions_path = context_dir / "DECISIONS.md"
    write_text_atomic(decisions_path, render_decisions_markdown(decisions))
    files_written["DECISIONS.md"] = str(decisions_path)

    relevant_files_path = context_dir / "RELEVANT_FILES.json"
    write_json_atomic(relevant_files_path, relevant_files)
    files_written["RELEVANT_FILES.json"] = str(relevant_files_path)

    blockers_path = context_dir / "BLOCKERS.json"
    write_json_atomic(blockers_path, blockers)
    files_written["BLOCKERS.json"] = str(blockers_path)

    session_summary_path = context_dir / "SESSION_SUMMARY.md"
    write_text_atomic(session_summary_path, render_session_summary(store))
    files_written["SESSION_SUMMARY.md"] = str(session_summary_path)

    session_audit_path = context_dir / "SESSION_AUDIT.json"
    write_json_atomic(session_audit_path, session_audit)
    files_written["SESSION_AUDIT.json"] = str(session_audit_path)

    resume_packet_path = context_dir / "RESUME_PACKET.md"
    write_text_atomic(
        resume_packet_path,
        (
            "# RESUME PACKET\n\n"
            "Use this file as the first recovery entry point when switching tools or recovering from an interrupted session.\n\n"
            f"- Current Task: {(current_task or {}).get('id', 'none')}\n"
            f"- Latest Handoff: {(latest_handoff or {}).get('id', 'none')}\n"
            f"- Active Sessions: {len(active_sessions)}\n"
        ),
    )
    files_written["RESUME_PACKET.md"] = str(resume_packet_path)

    write_json_atomic(json_export / "status_snapshot.json", snapshot)
    write_json_atomic(json_export / "active_tasks.json", store.get_active_tasks(limit=20))
    write_json_atomic(json_export / "latest_handoff.json", latest_handoff or {})
    write_json_atomic(json_export / "open_blockers.json", blockers)
    write_json_atomic(json_export / "active_sessions.json", active_sessions)
    write_json_atomic(json_export / "session_audit.json", session_audit)
    write_text_atomic(json_export / "compact_context.md", render_project_context(store))
    write_text_atomic(json_export / "session_summary.md", render_session_summary(store))

    return files_written
