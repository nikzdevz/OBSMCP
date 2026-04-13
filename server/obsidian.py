from __future__ import annotations

from datetime import datetime, timezone
import os
import re

from .config import AppConfig, ProjectConfig
from .semantic import render_semantic_note
from .store import StateStore
from .utils import read_text_with_retry, utc_now, write_text_atomic

# Sync marker — appended to every note; notes without this marker were externally edited
OBSIDIAN_SYNC_MARKER = "<!-- synclast: "


def _get_sync_time() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _inject_sync_marker(content: str) -> str:
    marker = f" {OBSIDIAN_SYNC_MARKER}{_get_sync_time()} -->\n"
    # Remove old marker and trailing newline
    content = re.sub(r"\s*" + re.escape(OBSIDIAN_SYNC_MARKER) + r".*?-->\n?", "\n", content).rstrip()
    return content + marker


def _get_last_sync(content: str) -> str | None:
    m = re.search(re.escape(OBSIDIAN_SYNC_MARKER) + r"([^>]+) --", content)
    return m.group(1) if m else None


def _render_project_brief(store: StateStore) -> str:
    brief = store.get_project_brief()
    lines = [
        "# Project Brief",
        "",
        "> Generated from obsmcp structured state. Edit through obsmcp tools or CLI to keep state consistent.",
        "",
    ]
    for section, content in brief.items():
        lines.extend([f"## {section}", "", content or "No content yet.", ""])
    return "\n".join(lines)


def _render_current_task(store: StateStore) -> str:
    task = store.get_current_task()
    if not task:
        return "# Current Task\n\nNo current task is set.\n"
    progress = store.get_checkpoint_progress(task["id"])
    checkpoints = store.get_checkpoints_for_task(task["id"], limit=store.config.checkpoints.render_limit)
    lines = [
        "# Current Task",
        "",
        f"- ID: {task['id']}",
        f"- Title: {task['title']}",
        f"- Status: {task['status']}",
        f"- Priority: {task['priority']}",
        f"- Owner: {task.get('owner') or 'unassigned'}",
        "",
        "## Description",
        "",
        task["description"],
        "",
        "## Relevant Files",
        "",
    ]
    if task["relevant_files"]:
        lines.extend(f"- {path}" for path in task["relevant_files"])
    else:
        lines.append("- None recorded")
    lines.extend(
        [
            "",
            "## Checkpoint Progress",
            "",
            (
                f"- Progress: {progress['completed_count']}/{progress['total_count']}"
                if progress.get("total_count") is not None
                else f"- Completed checkpoints: {progress['completed_count']}"
            ),
            f"- Latest checkpoint: {progress.get('latest_completed_at') or 'none'}",
            "",
            "## Phase Rollup",
            "",
        ]
    )
    if progress.get("phase_rollups"):
        for item in progress["phase_rollups"]:
            if item.get("total_count") is not None:
                lines.append(f"- {item['phase_key']}: {item['completed_count']}/{item['total_count']} complete")
            else:
                lines.append(f"- {item['phase_key']}: {item['completed_count']} complete")
    else:
        lines.append("- None recorded")
    lines.extend(
        [
            "",
            "## Recent Checkpoints",
            "",
        ]
    )
    if checkpoints:
        for item in checkpoints:
            lines.append(f"- {item['checkpoint_id']}: {item['title']} ({item['created_at']})")
    else:
        lines.append("- None recorded")
    lines.append("")
    return "\n".join(lines)


def _render_status_snapshot(store: StateStore) -> str:
    snapshot = store.get_project_status_snapshot()
    current_task = snapshot["current_task"]
    lines = [
        "# Status Snapshot",
        "",
        f"- App: {snapshot['app_name']}",
        f"- Current Task: {current_task['id'] if current_task else 'none'}",
        f"- Active Tasks: {len(snapshot['active_tasks'])}",
        f"- Open Blockers: {len(snapshot['blockers'])}",
        f"- Recent Decisions: {len(snapshot['decisions'])}",
        f"- Recent Checkpoints: {len(snapshot.get('recent_checkpoints', []))}",
        (
            f"- Current Task Progress: {snapshot['current_task_progress']['completed_count']}/{snapshot['current_task_progress']['total_count']}"
            if snapshot.get("current_task_progress") and snapshot["current_task_progress"].get("total_count") is not None
            else (
                f"- Current Task Completed Checkpoints: {snapshot['current_task_progress']['completed_count']}"
                if snapshot.get("current_task_progress")
                else "- Current Task Completed Checkpoints: none"
            )
        ),
        "",
        "## Relevant Files",
        "",
    ]
    if snapshot["relevant_files"]:
        lines.extend(f"- {path}" for path in snapshot["relevant_files"])
    else:
        lines.append("- None recorded")
    lines.extend(["", "## Recent Checkpoints", ""])
    if snapshot.get("recent_checkpoints"):
        for item in snapshot["recent_checkpoints"]:
            lines.append(f"- {item['checkpoint_id']}: {item['title']} ({item['created_at']})")
    else:
        lines.append("- None recorded")
    lines.append("")
    return "\n".join(lines)


def _render_latest_handoff(store: StateStore) -> str:
    handoff = store.get_latest_handoff()
    if not handoff:
        return "# Latest Handoff\n\nNo handoff recorded yet.\n"
    lines = [
        "# Latest Handoff",
        "",
        f"- ID: {handoff['id']}",
        f"- Task: {handoff['task_id'] or 'unassigned'}",
        f"- From: {handoff['from_actor']}",
        f"- To: {handoff['to_actor']}",
        f"- Created: {handoff['created_at']}",
        "",
        "## Summary",
        "",
        handoff["summary"],
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
        handoff["note"] or "None recorded.",
        "",
    ]
    return "\n".join(lines)


def _render_decision_index(store: StateStore) -> str:
    decisions = store.get_decisions(limit=50)
    lines = [
        "# Decision Log",
        "",
        "> Generated from obsmcp structured state. Individual ADR-style notes are generated below in the same folder.",
        "",
    ]
    if not decisions:
        lines.extend(["No decisions recorded yet.", ""])
        return "\n".join(lines)

    for item in decisions:
        lines.append(f"- ADR-{item['id']:04d}: {item['title']} ({item['created_at']})")
    lines.append("")
    return "\n".join(lines)


def _render_decision_note(item: dict[str, str]) -> str:
    return "\n".join(
        [
            f"# ADR-{item['id']:04d} {item['title']}",
            "",
            f"- Created: {item['created_at']}",
            f"- Actor: {item['actor']}",
            f"- Task: {item['task_id'] or 'unassigned'}",
            "",
            "## Decision",
            "",
            item["decision"] or "Not recorded.",
            "",
            "## Rationale",
            "",
            item["rationale"] or "Not recorded.",
            "",
            "## Impact",
            "",
            item["impact"] or "Not recorded.",
            "",
        ]
    )


def _render_architecture_map(store: StateStore) -> str:
    stats = store.get_symbol_index_stats()
    modules = store.get_cached_semantic_descriptions(entity_type="module", fresh_only=True, limit=8)
    lines = [
        "# Architecture Map",
        "",
        "> Machine-generated from the semantic knowledge cache. Use semantic MCP tools or `ctx describe ...` to refresh details.",
        "",
        "## Semantic Coverage",
        "",
    ]
    for key, value in stats.get("entity_counts", {}).items():
        lines.append(f"- {key.title()}: {value}")
    lines.extend(["", f"- Tracked files: {stats.get('tracked_files', 0)}", "", "## Key Modules", ""])
    if modules:
        for item in modules:
            lines.append(f"- `{item['file_path']}`: {item['purpose']}")
    else:
        lines.append("- No semantic module summaries cached yet.")
    lines.append("")
    return "\n".join(lines)


def _render_module_summaries(store: StateStore) -> str:
    modules = store.get_cached_semantic_descriptions(entity_type="module", fresh_only=True, limit=20)
    lines = ["# Module Summaries", ""]
    if not modules:
        lines.extend(["No module summaries cached yet.", ""])
        return "\n".join(lines)
    for item in modules:
        lines.extend(
            [
                f"## `{item['file_path']}`",
                "",
                item["purpose"],
                "",
                f"- Why: {item['why_it_exists']}",
                f"- Inputs/Outputs: {item['inputs_outputs']}",
                f"- Risks: {item['risks']}",
                "",
            ]
        )
    return "\n".join(lines)


def _render_feature_map(store: StateStore) -> str:
    features = store.get_cached_semantic_descriptions(entity_type="feature", fresh_only=True, limit=20)
    lines = ["# Feature Map", ""]
    if not features:
        lines.extend(["No feature summaries cached yet.", ""])
        return "\n".join(lines)
    for item in features:
        lines.extend(
            [
                f"## {item['name']}",
                "",
                item["purpose"],
                "",
                f"- Why: {item['why_it_exists']}",
                f"- Related Files: {', '.join(item['related_files']) or 'None'}",
                "",
            ]
        )
    return "\n".join(lines)


def sync_obsidian(config: AppConfig, store: StateStore, project_config: ProjectConfig) -> None:
    vault = project_config.vault_path
    for relative_dir in ["Projects", "Handoffs", "Decisions", "Daily", "Research", "Research/Symbol Knowledge", "Debug", "Sessions"]:
        (vault / relative_dir).mkdir(parents=True, exist_ok=True)

    # Check if Code Atlas exists and add its status to Research index
    atlas_path = vault / config.obsidian.code_atlas_note
    atlas_status_path = vault / "Research" / "Code Atlas Status.md"
    if atlas_path.exists():
        mtime = datetime.fromtimestamp(atlas_path.stat().st_mtime, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        atlas_content = _inject_sync_marker(f"""# Code Atlas

> The Code Atlas is a project-wide map of all source files, functions, classes, and features.

- **Atlas file:** `{config.obsidian.code_atlas_note}`
- **Last updated:** {mtime}

## Quick Reference

The Code Atlas documents:
- Every file in the project (scanned on demand)
- All functions and classes with signatures
- Imports and dependencies
- Language distribution
- Feature tags (e.g., "FastAPI", "React", "Redis")
- Largest files and code statistics

## Generating / Refreshing

Run `scan_codebase()` via MCP or `ctx.bat atlas refresh` to regenerate.

The atlas uses a **hybrid refresh strategy**: it only regenerates if a source file was modified after the atlas was last built, or if you force a refresh.

---
*Auto-generated by obsmcp*
""")
        write_text_atomic(atlas_status_path, atlas_content)

    write_text_atomic(vault / config.obsidian.project_brief_note, _inject_sync_marker(_render_project_brief(store)))
    write_text_atomic(vault / config.obsidian.current_task_note, _inject_sync_marker(_render_current_task(store)))
    write_text_atomic(vault / config.obsidian.status_snapshot_note, _inject_sync_marker(_render_status_snapshot(store)))
    write_text_atomic(vault / config.obsidian.latest_handoff_note, _inject_sync_marker(_render_latest_handoff(store)))
    write_text_atomic(vault / config.obsidian.decision_index_note, _inject_sync_marker(_render_decision_index(store)))

    for decision in store.get_decisions(limit=100):
        decision_path = vault / "Decisions" / f"ADR-{decision['id']:04d}.md"
        write_text_atomic(decision_path, _inject_sync_marker(_render_decision_note(decision)))

    write_text_atomic(vault / "Research" / "Architecture Map.md", _inject_sync_marker(_render_architecture_map(store)))
    write_text_atomic(vault / "Research" / "Module Summaries.md", _inject_sync_marker(_render_module_summaries(store)))
    write_text_atomic(vault / "Research" / "Feature Map.md", _inject_sync_marker(_render_feature_map(store)))

    for item in store.get_cached_semantic_descriptions(fresh_only=True, limit=40):
        name = item["entity_key"].replace(":", "__").replace("/", "_")
        note_path = vault / "Research" / "Symbol Knowledge" / f"{name}.md"
        write_text_atomic(note_path, _inject_sync_marker(render_semantic_note(item)))

    latest_summary = store.get_latest_session_summary()
    session_content = "# Latest Session Summary\n\nNo session summary recorded yet.\n"
    if latest_summary:
        session_content = _inject_sync_marker(
            "# Latest Session Summary\n\n"
            f"- Label: {latest_summary['session_label']}\n"
            f"- Actor: {latest_summary['actor']}\n"
            f"- Created: {latest_summary['created_at']}\n\n"
            f"{latest_summary['summary']}\n"
        )
    else:
        session_content = _inject_sync_marker("# Latest Session Summary\n\nNo session summary recorded yet.\n")
    write_text_atomic(vault / config.obsidian.session_note, session_content)

    for entry in store.get_daily_entries(limit=200):
        path = vault / config.obsidian.daily_notes_dir / f"{entry['note_date']}.md"
        existing = path.read_text(encoding="utf-8") if path.exists() else f"# {entry['note_date']}\n\n"
        marker = f"- {entry['created_at']} [{entry['actor']}] {entry['entry']}"
        if marker not in existing:
            content = existing.rstrip() + "\n" + marker + "\n"
            write_text_atomic(path, _inject_sync_marker(content))


# ------------------------------------------------------------------------------------------------
# Bidirectional: Pull changes from Obsidian back into obsmcp state
# ------------------------------------------------------------------------------------------------


def _parse_daily_entries_since(content: str, last_sync: str | None) -> list[dict[str, str]]:
    """
    Parse new daily entries appended after last_sync timestamp.
    Looks for lines like: `- 2026-04-12T10:30:00Z [actor] entry text`
    """
    entries: list[dict[str, str]] = []
    # Match bullet lines with ISO timestamp and actor tag
    pattern = re.compile(r"^\s*-\s*(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z?)\s*\[([^\]]+)\]\s*(.+)$")
    for line in content.splitlines():
        m = pattern.match(line)
        if not m:
            continue
        ts, actor, entry_text = m.group(1), m.group(2).strip(), m.group(3).strip()
        if last_sync and ts <= last_sync:
            continue
        entries.append({"entry": entry_text, "actor": actor, "created_at": ts})
    return entries


def _parse_project_brief_changes(content: str) -> dict[str, str]:
    """
    Parse the Project Brief note for manually-edited task descriptions.
    Extracts ## Task title lines into a dict for upserting into task state.
    """
    changes: dict[str, str] = {}
    current_section = ""
    current_body: list[str] = []
    for line in content.splitlines():
        if line.startswith("## "):
            if current_section:
                changes[current_section] = "\n".join(current_body).strip()
            current_section = line[3:].strip()
            current_body = []
        elif current_section:
            current_body.append(line)
    if current_section:
        changes[current_section] = "\n".join(current_body).strip()
    return changes


def pull_obsidian_changes(
    config: AppConfig,
    store: StateStore,
    project_config: ProjectConfig,
) -> dict[str, Any]:
    """
    Scan the Obsidian vault for externally-made changes since the last sync.
    Returns a dict describing what was pulled and applied.

    Conflict resolution:
    - Agent-written notes have <!-- synclast: timestamp --> marker
    - Notes without a marker or modified after the marker are treated as external edits
    - Daily entries appended after the sync marker are parsed and upserted
    """
    vault = project_config.vault_path
    pulled: dict[str, Any] = {
        "daily_entries": [],
        "decision_notes_found": 0,
        "project_brief_sections": {},
        "notes_scanned": 0,
    }

    # Pull daily entries from daily notes directory
    daily_dir = vault / config.obsidian.daily_notes_dir
    if daily_dir.is_dir():
        for note_path in sorted(daily_dir.glob("*.md")):
            try:
                content = read_text_with_retry(note_path)
            except OSError:
                continue
            pulled["notes_scanned"] += 1
            last_sync = _get_last_sync(content)
            new_entries = _parse_daily_entries_since(content, last_sync)
            for entry_data in new_entries:
                # Avoid duplicates by checking store
                existing = store.get_daily_entries(limit=500)
                already_there = any(
                    e["actor"] == entry_data["actor"] and e["entry"] == entry_data["entry"] and e.get("note_date", "").startswith(note_path.stem)
                    for e in existing
                )
                if not already_there:
                    date_str = note_path.stem  # YYYY-MM-DD
                    store.create_daily_note_entry(
                        entry=entry_data["entry"],
                        actor=entry_data["actor"],
                        note_date=date_str,
                    )
                    pulled["daily_entries"].append({**entry_data, "note_date": date_str})

    # Pull project brief section edits — check for externally-edited sections
    brief_path = vault / config.obsidian.project_brief_note
    if brief_path.exists():
        try:
            content = read_text_with_retry(brief_path)
            pulled["notes_scanned"] += 1
            last_sync = _get_last_sync(content)
            # Only pull if note was modified externally (no marker or marker older than file mtime)
            if last_sync is None:
                # Never synced — user created it externally
                section_changes = _parse_project_brief_changes(content)
                if section_changes:
                    pulled["project_brief_sections"] = section_changes
        except OSError:
            pass

    # Count new decision notes (ADR-*.md) not yet in store
    decisions_dir = vault / "Decisions"
    if decisions_dir.is_dir():
        for note_path in sorted(decisions_dir.glob("ADR-*.md")):
            try:
                content = read_text_with_retry(note_path)
            except OSError:
                continue
            pulled["notes_scanned"] += 1
            # Check if this ADR is already in the store
            id_str = note_path.stem  # e.g., ADR-0001
            existing_decisions = store.get_decisions(limit=500)
            already_exists = any(d.get("title", "").startswith(id_str.replace("-", " ")) for d in existing_decisions)
            if not already_exists:
                # New decision note created externally — flag for review
                pulled["decision_notes_found"] += 1

    return pulled
