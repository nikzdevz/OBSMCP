from __future__ import annotations

from pathlib import Path
from typing import Any

from .utils import write_text_atomic


def sync_hub_vault(hub_vault_path: Path, projects: list[dict[str, Any]]) -> list[str]:
    hub_vault_path.mkdir(parents=True, exist_ok=True)
    written: list[str] = []

    overview_lines = [
        "# obsmcp Hub",
        "",
        "Central dashboard for all registered obsmcp projects.",
        "",
        f"- Registered projects: {len(projects)}",
        "",
        "## Projects",
        "",
    ]
    for project in projects:
        overview_lines.extend(
            [
                f"### {project.get('name', project.get('slug'))}",
                "",
                f"- Slug: `{project.get('slug')}`",
                f"- Repo: `{project.get('repo_path')}`",
                f"- Workspace: `{project.get('workspace_path')}`",
                f"- Vault: `{project.get('vault_path')}`",
                f"- Last Active: {project.get('last_active_at', 'unknown')}",
                f"- Active Sessions: {project.get('active_session_count', 0)}",
                "",
            ]
        )
    overview_path = hub_vault_path / "Projects Overview.md"
    write_text_atomic(overview_path, "\n".join(overview_lines))
    written.append(str(overview_path))

    active_lines = [
        "# Active Projects",
        "",
        "Projects sorted by recent activity.",
        "",
    ]
    for project in projects:
        active_lines.append(
            f"- `{project.get('slug')}` {project.get('name')} | last_active={project.get('last_active_at')} | sessions={project.get('active_session_count', 0)}"
        )
    active_path = hub_vault_path / "Active Projects.md"
    write_text_atomic(active_path, "\n".join(active_lines) + "\n")
    written.append(str(active_path))

    return written
