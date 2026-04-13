from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .utils import read_json_with_retry, slugify, utc_now, write_json_atomic


def project_slug_for_path(project_path: str | Path) -> str:
    path = Path(project_path).resolve()
    base_slug = slugify(path.name or "project", max_length=32)
    digest = hashlib.sha1(str(path).encode("utf-8")).hexdigest()[:8]
    return f"{base_slug}-{digest}"


@dataclass
class ProjectRecord:
    slug: str
    name: str
    repo_path: str
    workspace_path: str
    vault_path: str
    context_path: str
    db_path: str
    created_at: str
    last_active_at: str
    tags: list[str] = field(default_factory=list)
    active_session_count: int = 0


class ProjectRegistry:
    def __init__(self, registry_path: Path) -> None:
        self.registry_path = registry_path
        self.registry_path.parent.mkdir(parents=True, exist_ok=True)

    def _load_payload(self) -> dict[str, Any]:
        return read_json_with_retry(self.registry_path, {"projects": []})

    def _save_payload(self, payload: dict[str, Any]) -> None:
        write_json_atomic(self.registry_path, payload)

    def list_projects(self) -> list[dict[str, Any]]:
        payload = self._load_payload()
        return sorted(payload.get("projects", []), key=lambda item: item.get("last_active_at", ""), reverse=True)

    def get_by_slug(self, project_slug: str) -> dict[str, Any] | None:
        for item in self.list_projects():
            if item.get("slug") == project_slug:
                return item
        return None

    def get_by_repo_path(self, repo_path: str | Path) -> dict[str, Any] | None:
        normalized = str(Path(repo_path).resolve())
        for item in self.list_projects():
            if item.get("repo_path") == normalized:
                return item
        return None

    def register(self, record: ProjectRecord) -> dict[str, Any]:
        payload = self._load_payload()
        projects = payload.setdefault("projects", [])
        existing_index = next((idx for idx, item in enumerate(projects) if item.get("slug") == record.slug), None)
        row = asdict(record)
        if existing_index is None:
            projects.append(row)
        else:
            projects[existing_index] = row
        self._save_payload(payload)
        return row

    def touch(
        self,
        project_slug: str,
        *,
        active_session_count: int | None = None,
        name: str | None = None,
        tags: list[str] | None = None,
    ) -> dict[str, Any] | None:
        payload = self._load_payload()
        projects = payload.setdefault("projects", [])
        now = utc_now()
        for item in projects:
            if item.get("slug") != project_slug:
                continue
            item["last_active_at"] = now
            if active_session_count is not None:
                item["active_session_count"] = active_session_count
            if name:
                item["name"] = name
            if tags is not None:
                item["tags"] = tags
            self._save_payload(payload)
            return item
        return None

    def resolve(self, project_slug: str | None = None, repo_path: str | Path | None = None) -> dict[str, Any] | None:
        if project_slug:
            return self.get_by_slug(project_slug)
        if repo_path:
            return self.get_by_repo_path(repo_path)
        return None
