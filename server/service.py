from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import threading
import time
import uuid
from concurrent.futures import Future, ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .code_atlas import CodeAtlas, generate_atlas, generate_atlas_markdown
from .compression import compress, compress_preserve_code
from .config import AppConfig, ProjectConfig
from .context_sync import render_handoff_markdown, sync_context_files
from .hub import sync_hub_vault
from .obsidian import pull_obsidian_changes, sync_obsidian
from .projects import ProjectRecord, ProjectRegistry
from .opusmax_provider import get_opusmax_tool_provider
from .output_policy import EffectiveOutputPolicy, resolve_output_policy
from .semantic import build_semantic_index, generate_semantic_description
from .store import StateStore
from .utils import is_port_open, read_json_with_retry, slugify, utc_now, write_json_atomic, write_text_atomic
from .observability import get_logger, span


class ObsmcpService:
    API_VERSION = "2026.04.14"
    TOOL_SCHEMA_VERSION = 2
    COMPATIBILITY_RULES_VERSION = 1
    _GLOBAL_TOOLS = {
        "register_project",
        "list_projects",
        "resolve_project",
        "resolve_active_project",
        "get_project_workspace_paths",
        "get_or_create_project",
        "sync_hub",
        "health_check",
        "list_tools",
        "list_resources",
        "generate_startup_prompt_template",
        "get_server_capabilities",
        "check_client_compatibility",
    }

    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.registry = ProjectRegistry(config.registry_path or config.root_dir / "registry" / "projects.json")
        self._stores: dict[str, StateStore] = {}
        self.store: StateStore | None = None
        self._scan_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="obsmcp-scan")
        self._scan_job_futures: dict[str, Future[Any]] = {}
        self._scan_jobs_lock = threading.Lock()
        self._precompute_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="obsmcp-precompute")
        self._precompute_jobs: dict[str, Future[Any]] = {}
        self._precompute_lock = threading.Lock()
        semantic_workers = max(1, int(self.config.semantic_auto_generate.max_concurrent_jobs))
        self._semantic_executor = ThreadPoolExecutor(max_workers=semantic_workers, thread_name_prefix="obsmcp-semantic")
        self._semantic_jobs: dict[str, Future[Any]] = {}
        self._semantic_jobs_lock = threading.Lock()
        self._sync_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="obsmcp-sync")
        self._sync_jobs: dict[str, Future[Any]] = {}
        self._sync_lock = threading.Lock()
        if self.config.bootstrap_default_project_on_startup:
            self.store = self._store_for(None)

    def _normalize_client_name(self, client_name: str | None) -> str:
        raw = (client_name or "").strip().lower().replace("_", "-").replace(" ", "-")
        aliases = {
            "claude-code": "claude-code-vscode",
            "vscode-claude-code": "claude-code-vscode",
            "claude-vscode": "claude-code-vscode",
            "codex": "vscode-codex",
            "codex-vscode": "vscode-codex",
        }
        return aliases.get(raw, raw)

    def _normalize_model_name(self, model_name: str | None) -> str:
        raw = (model_name or "").strip().lower().replace("_", "-").replace(" ", "-")
        aliases = {
            "opus-4.6": "claude-opus-4-6",
            "claude-opus-4.6": "claude-opus-4-6",
            "gpt5": "gpt-5",
        }
        return aliases.get(raw, raw)

    def _tokenize_similarity_text(self, value: str) -> set[str]:
        return {
            token
            for token in re.findall(r"[a-z0-9]{3,}", (value or "").lower())
            if token not in {"task", "work", "session", "project", "please", "create", "open", "resume"}
        }

    def _jaccard_similarity(self, left: str, right: str) -> float:
        left_tokens = self._tokenize_similarity_text(left)
        right_tokens = self._tokenize_similarity_text(right)
        if not left_tokens or not right_tokens:
            return 0.0
        union = left_tokens | right_tokens
        if not union:
            return 0.0
        return len(left_tokens & right_tokens) / len(union)

    def _derive_session_label(self, initial_request: str, session_goal: str, task: dict[str, Any] | None = None) -> str:
        text = " ".join(part.strip() for part in [initial_request, session_goal] if part and part.strip())

        def _humanize_label(candidate: str) -> str:
            normalized = re.sub(r"\s+", " ", candidate).strip(" .:-")
            if not normalized:
                return ""
            normalized = re.sub(r"^(?:the|a|an)\s+", "", normalized, flags=re.IGNORECASE)
            return re.sub(
                r"[A-Za-z][A-Za-z']*",
                lambda match: match.group(0)[:1].upper() + match.group(0)[1:],
                normalized,
            )

        patterns = [
            r"\b(?:this is|it is|task is|session is)\s+(?:a\s+)?task\s+for\s+([^.,;\n]{4,80})",
            r"\b(?:task|session|workstream)\s+for\s+([^.,;\n]{4,80})",
            r"\b(?:call|name|label)\s+(?:this\s+)?(?:task|session|workstream)\s+([^.,;\n]{4,80})",
        ]
        for pattern in patterns:
            match = re.search(pattern, text, flags=re.IGNORECASE)
            if match:
                candidate = _humanize_label(match.group(1))
                if candidate:
                    return candidate
        if task and task.get("title"):
            return str(task["title"]).strip()[:96]
        summary_source = initial_request.strip() or session_goal.strip()
        if not summary_source:
            return "Untitled Session"
        summary = summary_source.split(".")[0].split("\n")[0].strip()
        summary = re.sub(r"^(please|kindly|now|today)\s+", "", summary, flags=re.IGNORECASE)
        return summary[:96] or "Untitled Session"

    def _derive_session_identity(
        self,
        *,
        initial_request: str,
        session_goal: str,
        task: dict[str, Any] | None,
        session_label: str,
        workstream_key: str,
        workstream_title: str,
    ) -> dict[str, str]:
        resolved_label = (session_label or "").strip() or self._derive_session_label(initial_request, session_goal, task=task)
        resolved_workstream_title = (workstream_title or "").strip() or (task.get("title", "").strip() if task else "") or resolved_label
        workstream_source = re.sub(r"[']", "", resolved_workstream_title or resolved_label)
        resolved_workstream_key = (workstream_key or "").strip() or slugify(workstream_source, max_length=48) or "default-workstream"
        return {
            "session_label": resolved_label,
            "workstream_key": resolved_workstream_key,
            "workstream_title": resolved_workstream_title,
        }

    def _request_needs_task_anchor(self, initial_request: str, session_goal: str) -> bool:
        combined = " ".join(part.strip() for part in [initial_request, session_goal] if part and part.strip())
        if len(combined) >= 60:
            return True
        verbs = {"implement", "create", "build", "write", "refactor", "debug", "fix", "analyze", "design"}
        return any(token in combined.lower() for token in verbs)

    def _session_resume_mismatch_reason(
        self,
        candidate: dict[str, Any],
        *,
        task_id: str | None,
        initial_request: str,
        session_goal: str,
        session_label: str,
        workstream_key: str,
    ) -> str | None:
        if task_id and candidate.get("task_id") and candidate.get("task_id") != task_id:
            return "candidate session belongs to a different task"
        if workstream_key and candidate.get("workstream_key") and candidate.get("workstream_key") != workstream_key:
            return "candidate session belongs to a different workstream"
        incoming_label = (session_label or "").strip().lower()
        candidate_label = str(candidate.get("session_label") or "").strip().lower()
        if incoming_label and candidate_label and incoming_label != candidate_label and workstream_key:
            return "candidate session label does not match the requested workstream"
        incoming_text = " ".join(part.strip() for part in [initial_request, session_goal, session_label] if part and part.strip())
        candidate_text = " ".join(
            str(candidate.get(key, "")).strip()
            for key in ("initial_request", "session_goal", "session_label", "workstream_title")
            if candidate.get(key)
        )
        if not incoming_text or not candidate_text:
            return None
        if self._request_needs_task_anchor(initial_request, session_goal) and self._jaccard_similarity(incoming_text, candidate_text) < 0.15:
            return "candidate session goal conflicts with the incoming request"
        return None

    def _build_session_open_warnings(
        self,
        *,
        task: dict[str, Any] | None,
        task_id: str | None,
        initial_request: str,
        session_goal: str,
        latest_handoff: dict[str, Any] | None,
    ) -> list[str]:
        warnings: list[str] = []
        if not task_id and self._request_needs_task_anchor(initial_request, session_goal):
            warnings.append("No task is attached to this substantial session. Create or select a task to keep continuity clean.")
        if task and task.get("status") == "done":
            warnings.append("The attached current task is already marked done.")
        if latest_handoff and task_id and latest_handoff.get("task_id") and latest_handoff.get("task_id") != task_id:
            warnings.append("The latest handoff belongs to a different task than the requested session.")
        return warnings

    def _resolve_project(self, project_path: str | None = None, project_slug: str | None = None) -> ProjectConfig:
        if project_slug:
            record = self.registry.get_by_slug(project_slug)
            if not record:
                raise ValueError(f"Unknown project slug: {project_slug}")
            project_path = record["repo_path"]
        pconfig = self.config.get_project_config(project_path, project_slug=project_slug)
        self._register_project_config(pconfig)
        return pconfig

    def _register_project_config(self, project_config: ProjectConfig) -> dict[str, Any]:
        now = utc_now()
        payload = {
            "project_slug": project_config.project_slug,
            "project_name": project_config.project_name,
            "repo_path": str(project_config.project_path),
            "workspace_path": str(project_config.workspace_root),
            "vault_path": str(project_config.vault_path),
            "context_path": str(project_config.context_path),
            "db_path": str(project_config.db_path),
            "created_at": now,
            "last_active_at": now,
        }
        existing = self.registry.get_by_slug(project_config.project_slug)
        if existing:
            payload["created_at"] = existing.get("created_at", now)
            payload["active_session_count"] = existing.get("active_session_count", 0)
            payload["tags"] = existing.get("tags", [])
        write_json_atomic(project_config.manifest_path, payload)
        return self.registry.register(
            record=ProjectRecord(
                slug=project_config.project_slug,
                name=project_config.project_name,
                repo_path=str(project_config.project_path),
                workspace_path=str(project_config.workspace_root),
                vault_path=str(project_config.vault_path),
                context_path=str(project_config.context_path),
                db_path=str(project_config.db_path),
                created_at=payload["created_at"],
                last_active_at=payload["last_active_at"],
                tags=payload.get("tags", []),
                active_session_count=payload.get("active_session_count", 0),
            )
        )

    def _resolve_project_config(self, project_path: str | None, project_slug: str | None = None) -> ProjectConfig:
        """Resolve project config: explicit path/slug, env var, or default."""
        return self._resolve_project(project_path=project_path, project_slug=project_slug)

    def _store_for(self, project_path: str | None, project_slug: str | None = None) -> StateStore:
        """Get or create a StateStore for the given project path."""
        pconfig = self._resolve_project_config(project_path, project_slug=project_slug)
        key = str(pconfig.project_path.resolve())
        if key not in self._stores:
            self._stores[key] = StateStore(self.config, pconfig)
        self.registry.touch(pconfig.project_slug, active_session_count=len(self._stores[key].get_active_sessions(limit=100)))
        return self._stores[key]

    def _store(self, project_path: str | None = None, project_slug: str | None = None) -> StateStore:
        """Get the store for a project, lazily creating the default store only when needed."""
        if project_path or project_slug:
            return self._store_for(project_path, project_slug=project_slug)
        if self.store is None:
            self.store = self._store_for(None)
        return self.store

    def _project_config_for(self, project_path: str | None, project_slug: str | None = None) -> ProjectConfig:
        return self._resolve_project_config(project_path, project_slug=project_slug)

    def _known_project_paths(self) -> list[str]:
        known = set(self._stores.keys())
        for project in self.registry.list_projects():
            if project.get("repo_path"):
                known.add(project["repo_path"])
        return sorted(known)

    def _project_path_from_bridge(self, candidate: Path) -> str | None:
        bridge_path = candidate / ".obsmcp-link.json"
        if not bridge_path.exists():
            return None
        payload = read_json_with_retry(bridge_path, {})
        target = payload.get("project_path") or payload.get("repo_path")
        if not target:
            return None
        return str(Path(target).resolve())

    def _registered_project_for_path_hint(self, path_hint: str | Path | None) -> str | None:
        if not path_hint:
            return None
        candidate = Path(path_hint).expanduser()
        resolved = candidate.resolve(strict=False)
        if not resolved.exists() and resolved.suffix:
            resolved = resolved.parent
        elif resolved.exists() and resolved.is_file():
            resolved = resolved.parent

        for parent in [resolved, *resolved.parents]:
            bridge_target = self._project_path_from_bridge(parent)
            if bridge_target:
                return bridge_target

        best_match: str | None = None
        best_depth = -1
        for project in self.registry.list_projects():
            repo_path = project.get("repo_path")
            if not repo_path:
                continue
            repo = Path(repo_path).resolve(strict=False)
            try:
                resolved.relative_to(repo)
            except ValueError:
                continue
            depth = len(repo.parts)
            if depth > best_depth:
                best_depth = depth
                best_match = str(repo)
        if best_match:
            return best_match

        for parent in [resolved, *resolved.parents]:
            if (parent / ".git").exists():
                return str(parent)
        if resolved.exists():
            return str(resolved)
        return None

    def _extract_project_path_hints(self, arguments: dict[str, Any]) -> list[str]:
        hints: list[str] = []
        for key in ("repo_path", "cwd", "workspace_path", "file_path", "path", "repo_root", "project_root"):
            value = arguments.get(key)
            if isinstance(value, str) and value.strip():
                hints.append(value)
        for key in ("files", "relevant_files"):
            value = arguments.get(key)
            if isinstance(value, list):
                hints.extend([item for item in value if isinstance(item, str) and item.strip()])
        return hints

    def _find_project_path_for_session(self, session_id: str | None) -> str | None:
        if not session_id:
            return None
        for project_path in self._known_project_paths():
            session = self._store_for(project_path).get_session(session_id)
            if session:
                return project_path
        return None

    def _find_project_path_for_task(self, task_id: str | None) -> str | None:
        if not task_id:
            return None
        for project_path in self._known_project_paths():
            task = self._store_for(project_path).get_task(task_id)
            if task:
                return project_path
        return None

    def _resolve_nearest_git_root(self, path_hint: str | None, max_depth: int = 5) -> str | None:
        """Walk upward from path_hint up to max_depth levels looking for a .git directory or file."""
        if not path_hint:
            return None
        candidate = Path(path_hint).expanduser().resolve(strict=False)
        if candidate.is_file():
            candidate = candidate.parent
        for i, parent in enumerate([candidate, *candidate.parents]):
            if i > max_depth:
                break
            git_dir = parent / ".git"
            if git_dir.exists() and git_dir.is_dir():
                return str(parent)
            try:
                content = git_dir.read_text(encoding="utf-8").strip()
                if content.startswith("gitdir:"):
                    gitdir = content.split(":", 1)[1].strip()
                    resolved = (parent / gitdir).resolve(strict=False)
                    return str(resolved.parent)
            except (OSError, ValueError):
                pass
        return None

    def _infer_project_path(self, arguments: dict[str, Any]) -> str | None:
        explicit_slug = arguments.get("project_slug")
        if explicit_slug:
            record = self.registry.get_by_slug(explicit_slug)
            if record:
                return record["repo_path"]

        explicit = arguments.get("project_path")
        if explicit:
            return explicit

        repo_path = arguments.get("repo_path")
        if isinstance(repo_path, str) and repo_path.strip():
            return self._registered_project_for_path_hint(repo_path) or repo_path

        session_project = self._find_project_path_for_session(arguments.get("session_id"))
        if session_project:
            return session_project

        task_project = self._find_project_path_for_task(arguments.get("task_id"))
        if task_project:
            return task_project

        for hint in self._extract_project_path_hints(arguments):
            inferred = self._registered_project_for_path_hint(hint)
            if inferred:
                return inferred

        env_project = os.environ.get("OBSMCP_PROJECT")
        if env_project:
            return self._registered_project_for_path_hint(env_project) or env_project

        cwd = os.getcwd()
        if cwd:
            git_root = self._resolve_nearest_git_root(cwd)
            if git_root:
                registered = self._registered_project_for_path_hint(git_root)
                return registered or git_root

        return None

    def _tool_requires_project_context(self, name: str) -> bool:
        return name not in self._GLOBAL_TOOLS

    def _missing_project_context_error(self, name: str) -> ValueError:
        return ValueError(
            "Project context is required for "
            f"'{name}'. Pass one of: project_path, project_slug, session_id, task_id, "
            "repo_path, cwd, file_path, path, or files/relevant_files. "
            "Alternatively, call session_open with a project hint first, or call "
            "get_or_create_project/resolve_project to resolve the project explicitly."
        )

    def sync_hub(self) -> dict[str, Any]:
        files = sync_hub_vault(self.config.hub_vault_dir or self.config.root_dir / "hub" / "vault", self.registry.list_projects())
        return {"synced": True, "files": files}

    def sync_all(self, project_path: str | None = None, project_slug: str | None = None) -> dict[str, Any]:
        with span("sync_all", project_path=project_path, project_slug=project_slug):
            pcfg = self._project_config_for(project_path, project_slug=project_slug)
            store = self._store(project_path, project_slug=project_slug)
            files = sync_context_files(self.config, store, pcfg)
            pulled = pull_obsidian_changes(self.config, store, pcfg)
            sync_obsidian(self.config, store, pcfg)
            resume = self.generate_resume_packet(project_path=str(pcfg.project_path), write_files=True)
            artifact_files = self._sync_context_artifacts(project_path=str(pcfg.project_path))
            self.registry.touch(pcfg.project_slug, active_session_count=len(store.get_active_sessions(limit=100)))
            hub_result = self.sync_hub()
            self._submit_precompute(str(pcfg.project_path))
            return {
                "synced": True,
                "files": {**files, **artifact_files},
                "resume_packet": resume["path"],
                "hub_files": hub_result["files"],
                "pulled": pulled,
            }

    def _run_deferred_sync(self, project_path: str) -> dict[str, Any]:
        try:
            return self.sync_all(project_path=project_path)
        finally:
            with self._sync_lock:
                self._sync_jobs.pop(f"sync:{project_path}", None)

    def _submit_deferred_sync(self, project_path: str) -> None:
        with self._sync_lock:
            job_key = f"sync:{project_path}"
            if job_key in self._sync_jobs and not self._sync_jobs[job_key].done():
                return
            self._sync_jobs[job_key] = self._sync_executor.submit(self._run_deferred_sync, project_path)

    def _sync_after_write(self, project_path: str | None, sync_mode: str = "full") -> Any:
        effective_project_path = str(self._project_config_for(project_path).project_path)
        normalized = (sync_mode or "full").lower()
        if normalized == "none":
            return {"synced": False, "mode": "none", "project_path": effective_project_path}
        if normalized == "deferred":
            self._submit_deferred_sync(effective_project_path)
            return {"synced": False, "mode": "deferred", "project_path": effective_project_path}
        result = self.sync_all(effective_project_path)
        if isinstance(result, dict):
            result["mode"] = "full"
        return result

    def _atlas_excluded_roots(self, project_path: str | None = None) -> list[Path]:
        pcfg = self._project_config_for(project_path)
        root = pcfg.project_path.resolve()
        candidates = [
            pcfg.workspace_root,
            pcfg.context_path,
            pcfg.vault_path,
            pcfg.log_dir,
            pcfg.data_root,
            pcfg.json_export_dir,
            pcfg.backup_dir,
            pcfg.export_dir,
            root / ".obsmcp",
            root / ".context",
            root / "obsidian",
        ]
        if root == self.config.root_dir.resolve():
            candidates.extend(
                [
                    self.config.root_dir / "projects",
                    self.config.root_dir / "registry",
                    self.config.root_dir / "hub",
                    self.config.root_dir / "logs",
                    self.config.root_dir / "data",
                    self.config.root_dir / "obsidian",
                ]
            )
        excluded: list[Path] = []
        for candidate in candidates:
            try:
                resolved = candidate.resolve()
                if resolved == root:
                    continue
                resolved.relative_to(root)
                excluded.append(resolved)
            except ValueError:
                continue
            except OSError:
                continue
        return excluded

    def _build_code_atlas(self, project_path: str | None = None) -> CodeAtlas:
        pcfg = self._project_config_for(project_path)
        return CodeAtlas(pcfg.project_path, excluded_roots=self._atlas_excluded_roots(project_path))

    def _current_atlas_metadata(self, project_path: str | None = None) -> dict[str, Any]:
        pcfg = self._project_config_for(project_path)
        atlas_path = pcfg.vault_path / "Research" / "Code Atlas.md"
        semantic_stats = self._store(project_path).get_symbol_index_stats()
        cached = read_json_with_retry(pcfg.json_export_dir / "code_atlas.json", {})
        if cached:
            return {
                "status": "current",
                "message": "Code Atlas is up to date.",
                "total_files": cached.get("total_files", 0),
                "total_lines": cached.get("total_lines", 0),
                "languages": cached.get("languages", {}),
                "generated_at": cached.get("generated_at"),
                "atlas_path": str(atlas_path),
                "semantic_index": semantic_stats,
            }
        atlas = self._build_code_atlas(project_path)
        result = atlas.scan()
        return {
            "status": "current",
            "message": "Code Atlas is up to date.",
            "total_files": result.total_files,
            "total_lines": result.total_lines,
            "languages": result.languages,
            "generated_at": result.generated_at,
            "atlas_path": str(atlas_path),
            "semantic_index": semantic_stats,
        }

    def _atlas_needs_refresh(self, project_path: str | None = None, force_refresh: bool = False) -> bool:
        if force_refresh:
            return True
        pcfg = self._project_config_for(project_path)
        atlas_path = pcfg.vault_path / "Research" / "Code Atlas.md"
        if not atlas_path.exists():
            return True
        atlas_mtime = atlas_path.stat().st_mtime
        root = pcfg.project_path
        exclude_dirs = {".venv", "venv", "node_modules", ".git", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", "dist", "build", "target", ".next", ".nuxt", ".cache", "tmp", "temp", ".tmp"}
        excluded_roots = {item.resolve() for item in self._atlas_excluded_roots(project_path)}
        for dirpath, dirnames, filenames in os.walk(root):
            root_path = Path(dirpath).resolve()
            dirnames[:] = [
                d
                for d in dirnames
                if d not in exclude_dirs
                and not d.startswith(".")
                and (root_path / d).resolve() not in excluded_roots
                and not any((root_path / d).resolve().is_relative_to(item) for item in excluded_roots)
            ]
            if any(root_path.is_relative_to(item) for item in excluded_roots):
                continue
            for filename in filenames:
                filepath = root_path / filename
                try:
                    if any(filepath.resolve().is_relative_to(item) for item in excluded_roots):
                        continue
                    if filepath.stat().st_mtime > atlas_mtime:
                        return True
                except OSError:
                    pass
        return False

    def _scan_codebase_sync(self, project_path: str | None = None, force_refresh: bool = False) -> dict[str, Any]:
        pcfg = self._project_config_for(project_path)
        atlas_path = pcfg.vault_path / "Research" / "Code Atlas.md"
        atlas_path.parent.mkdir(parents=True, exist_ok=True)

        should_refresh = self._atlas_needs_refresh(project_path, force_refresh=force_refresh)

        if not should_refresh:
            return self._current_atlas_metadata(project_path)

        atlas = self._build_code_atlas(project_path)
        result = atlas.scan()
        markdown = atlas.generate_markdown(result)
        write_text_atomic(atlas_path, markdown)
        _, _, _, semantic_stats = self._refresh_semantic_index(project_path=str(pcfg.project_path), atlas_result=result)
        semantic_prewarm = self._submit_semantic_prewarm(
            [*semantic_stats.get("changed_files", []), *semantic_stats.get("added_files", [])],
            project_path=str(pcfg.project_path),
            reason="scan_codebase",
            limit=self.config.semantic_auto_generate.max_modules_per_scan,
        )
        write_json_atomic(pcfg.json_export_dir / "code_atlas.json", result.to_dict())
        return {
            "status": "generated",
            "message": "Code Atlas generated successfully.",
            "total_files": result.total_files,
            "total_lines": result.total_lines,
            "languages": result.languages,
            "generated_at": result.generated_at,
            "atlas_path": str(atlas_path),
            "file_count": result.total_files,
            "semantic_index": semantic_stats,
            "semantic_prewarm": semantic_prewarm,
        }

    def _run_scan_job(self, job_id: str, project_path: str, force_refresh: bool) -> None:
        store = self._store(project_path)
        started_at = utc_now()
        store.update_scan_job(job_id, status="running", started_at=started_at, progress_message="Scanning codebase and refreshing semantic index.")
        try:
            result = self._scan_codebase_sync(project_path=project_path, force_refresh=force_refresh)
            store.update_scan_job(
                job_id,
                status="completed",
                finished_at=utc_now(),
                progress_message="Scan completed successfully.",
                result=result,
            )
        except Exception as exc:
            store.update_scan_job(
                job_id,
                status="failed",
                finished_at=utc_now(),
                progress_message="Scan failed.",
                error_text=str(exc),
            )
        finally:
            with self._scan_jobs_lock:
                self._scan_job_futures.pop(job_id, None)

    def start_scan_job(
        self,
        project_path: str | None = None,
        *,
        force_refresh: bool = False,
        requested_by: str = "unknown",
    ) -> dict[str, Any]:
        resolved_path = str(self._project_config_for(project_path).project_path)
        store = self._store(resolved_path)
        active = store.get_active_scan_job(job_type="code_atlas")
        if active:
            active["poll_hint"] = "Call get_scan_job with this job_id until status is completed or failed."
            return active
        job_id = f"SCAN-{uuid.uuid4().hex[:12].upper()}"
        job = store.create_scan_job(
            job_id,
            job_type="code_atlas",
            project_path=resolved_path,
            requested_by=requested_by,
            force_refresh=force_refresh,
        )
        with self._scan_jobs_lock:
            self._scan_job_futures[job_id] = self._scan_executor.submit(self._run_scan_job, job_id, resolved_path, force_refresh)
        job["message"] = "Scan queued in background."
        job["poll_hint"] = "Call get_scan_job with this job_id until status is completed or failed."
        return job

    def get_scan_job(self, job_id: str, project_path: str | None = None) -> dict[str, Any]:
        job = self._store(project_path).get_scan_job(job_id)
        if not job:
            raise ValueError(f"Unknown scan job: {job_id}")
        if job["status"] in {"queued", "running"}:
            job["poll_hint"] = "Poll this job until status is completed or failed."
        return job

    def list_scan_jobs(self, project_path: str | None = None, status: str | None = None, limit: int = 20) -> dict[str, Any]:
        jobs = self._store(project_path).list_scan_jobs(status=status, limit=limit)
        return {"jobs": jobs, "count": len(jobs)}

    def wait_for_scan_job(self, job_id: str, project_path: str | None = None, wait_seconds: int = 30, poll_interval_seconds: float = 0.5) -> dict[str, Any]:
        deadline = time.time() + max(wait_seconds, 0)
        while True:
            job = self.get_scan_job(job_id, project_path=project_path)
            if job["status"] in {"completed", "failed", "interrupted"}:
                return job
            if time.time() >= deadline:
                job["timed_out"] = True
                return job
            time.sleep(max(poll_interval_seconds, 0.1))

    def _refresh_semantic_index(
        self,
        project_path: str | None = None,
        atlas_result: Any | None = None,
    ) -> tuple[ProjectConfig, Any, Any, dict[str, Any]]:
        pcfg = self._project_config_for(project_path)
        result = atlas_result or self._build_code_atlas(project_path).scan()
        index = build_semantic_index(pcfg.project_path, result)
        payload = index.build_index_payload()
        stats = self._store(project_path).replace_semantic_index(payload["entities"], payload["file_fingerprints"])
        write_json_atomic(pcfg.json_export_dir / "semantic_symbol_index.json", payload)
        write_text_atomic(pcfg.json_export_dir / "semantic_symbol_index.md", index.render_summary_markdown())
        return pcfg, result, index, stats

    def _describe_entity(
        self,
        entity: dict[str, Any],
        index: Any,
        project_path: str | None = None,
        write_sync: bool = True,
        force_llm: bool = False,
        allow_llm: bool = True,
        force_refresh: bool = False,
    ) -> dict[str, Any]:
        store = self._store(project_path)
        cached = store.get_semantic_description(entity["entity_key"])
        if not force_refresh and cached and not cached.get("stale") and cached.get("source_fingerprint") == entity["source_fingerprint"]:
            cached["freshness"] = "fresh"
            cached["cached"] = True
            return cached
        operation_kind = "architecture" if entity.get("entity_type") in {"module", "feature"} else "general"
        policy = self._resolve_output_policy(
            operation_kind=operation_kind,
            project_path=project_path,
        )
        description = generate_semantic_description(
            entity,
            index,
            store,
            Path(self._project_config_for(project_path).project_path),
            force_llm=force_llm,
            allow_llm=allow_llm,
            response_contract=policy.prompt_contract if policy.mode == "gateway_enforced" else None,
        )
        description["symbol_path"] = entity["symbol_path"]
        saved = store.upsert_semantic_description(description)
        saved["cached"] = False
        self._record_output_policy_metric(
            operation="generate_semantic_description",
            policy=policy,
            rendered_text=json.dumps(saved, ensure_ascii=True),
            project_path=project_path,
        )
        if write_sync:
            self.sync_all(project_path)
        return saved

    def _is_low_value_semantic_path(self, file_path: str) -> bool:
        normalized = file_path.replace("\\", "/").lower()
        if any(fragment.lower() in normalized for fragment in self.config.semantic_auto_generate.skip_path_fragments):
            return True
        file_name = normalized.rsplit("/", 1)[-1]
        return any(file_name.endswith(suffix.lower()) for suffix in self.config.semantic_auto_generate.skip_generated_suffixes)

    def _collect_semantic_candidate_files(
        self,
        explicit_files: list[str] | None,
        *,
        task_id: str | None = None,
        project_path: str | None = None,
        limit: int | None = None,
    ) -> list[str]:
        store = self._store(project_path)
        task = store.get_task(task_id) if task_id else store.get_current_task()
        max_candidates = max((limit or self.config.semantic_auto_generate.max_modules_per_write) * 4, 12)
        ordered: list[str] = []
        seen: set[str] = set()

        def extend(paths: list[str] | None) -> None:
            for item in paths or []:
                if not isinstance(item, str) or not item.strip():
                    continue
                normalized = item.replace("\\", "/")
                if normalized in seen or self._is_low_value_semantic_path(normalized):
                    continue
                seen.add(normalized)
                ordered.append(normalized)
                if len(ordered) >= max_candidates:
                    return

        extend(explicit_files)
        if task:
            extend(task.get("relevant_files", []))
        extend(store.get_relevant_files(task_id=task["id"] if task else task_id, limit=max_candidates))
        if len(ordered) < max_candidates:
            extend(store.get_recent_file_activity(limit=max_candidates))
        return ordered[:max_candidates]

    def _is_module_description_fresh(self, module: dict[str, Any], project_path: str | None = None) -> bool:
        store = self._store(project_path)
        cached = store.get_semantic_description(module["entity_key"])
        return bool(cached and not cached.get("stale") and cached.get("source_fingerprint") == module.get("source_fingerprint"))

    def _module_candidates_for_files(
        self,
        file_paths: list[str] | None,
        *,
        task_id: str | None = None,
        project_path: str | None = None,
        limit: int | None = None,
        refresh_if_missing: bool = True,
    ) -> list[str]:
        candidates = self._collect_semantic_candidate_files(file_paths, task_id=task_id, project_path=project_path, limit=limit)
        if not candidates:
            return []
        store = self._store(project_path)
        selected: list[str] = []
        seen: set[str] = set()
        for file_path in candidates:
            module = store.get_module_index(file_path)
            if not module:
                continue
            normalized = module["file_path"]
            if normalized in seen or self._is_low_value_semantic_path(normalized):
                continue
            if self._is_module_description_fresh(module, project_path=project_path):
                continue
            seen.add(normalized)
            selected.append(normalized)
            if limit is not None and len(selected) >= limit:
                break
        if not selected and refresh_if_missing:
            try:
                self._refresh_semantic_index(project_path)
            except Exception:
                return []
            store = self._store(project_path)
            for file_path in candidates:
                module = store.get_module_index(file_path)
                if not module:
                    continue
                normalized = module["file_path"]
                if normalized in seen or self._is_low_value_semantic_path(normalized):
                    continue
                if self._is_module_description_fresh(module, project_path=project_path):
                    continue
                seen.add(normalized)
                selected.append(normalized)
                if limit is not None and len(selected) >= limit:
                    break
        return selected

    def _wait_for_semantic_job(self, job_key: str, timeout_ms: int) -> dict[str, Any]:
        if timeout_ms <= 0:
            return {"waited": False, "timed_out": False}
        with self._semantic_jobs_lock:
            future = self._semantic_jobs.get(job_key)
        if future is None:
            return {"waited": False, "timed_out": False}
        try:
            result = future.result(timeout=timeout_ms / 1000.0)
            return {"waited": True, "timed_out": False, "result": result}
        except FuturesTimeoutError:
            return {"waited": True, "timed_out": True}

    def _best_effort_semantic_prewarm(
        self,
        file_paths: list[str] | None,
        *,
        task_id: str | None = None,
        project_path: str | None = None,
        reason: str = "",
        limit: int | None = None,
        allow_llm: bool | None = None,
        wait_ms: int = 0,
    ) -> dict[str, Any]:
        result = self._submit_semantic_prewarm(
            file_paths,
            task_id=task_id,
            project_path=project_path,
            reason=reason,
            limit=limit,
            allow_llm=allow_llm,
        )
        job_key = result.get("job_key")
        if job_key:
            result.update(self._wait_for_semantic_job(job_key, timeout_ms=wait_ms))
        return result

    def _prewarm_module_descriptions(
        self,
        file_paths: list[str] | None,
        *,
        task_id: str | None = None,
        project_path: str | None = None,
        limit: int | None = None,
        allow_llm: bool | None = None,
        sync_after: bool = True,
    ) -> dict[str, Any]:
        limit_value = limit if limit is not None else self.config.semantic_auto_generate.max_modules_per_scan
        module_paths = self._module_candidates_for_files(file_paths, task_id=task_id, project_path=project_path, limit=limit_value)
        if not module_paths:
            return {"requested_files": file_paths or [], "module_paths": [], "generated": 0, "cached": 0}

        pcfg, _, index, _ = self._refresh_semantic_index(project_path)
        generated = 0
        cached = 0
        llm_allowed = self.config.semantic_auto_generate.allow_llm if allow_llm is None else allow_llm
        for module_path in module_paths:
            entity = index.get_module(module_path)
            if not entity:
                continue
            result = self._describe_entity(
                entity.to_index_row(),
                index,
                project_path=str(pcfg.project_path),
                write_sync=False,
                allow_llm=llm_allowed,
            )
            if result.get("cached"):
                cached += 1
            else:
                generated += 1
        if sync_after and (generated or cached):
            self.sync_all(str(pcfg.project_path))
        return {"requested_files": file_paths or [], "module_paths": module_paths, "generated": generated, "cached": cached}

    def _run_semantic_prewarm(
        self,
        job_key: str,
        project_path: str,
        file_paths: list[str],
        task_id: str | None,
        limit: int,
        allow_llm: bool,
    ) -> dict[str, Any]:
        try:
            return self._prewarm_module_descriptions(
                file_paths,
                task_id=task_id,
                project_path=project_path,
                limit=limit,
                allow_llm=allow_llm,
                sync_after=True,
            )
        finally:
            with self._semantic_jobs_lock:
                self._semantic_jobs.pop(job_key, None)

    def _submit_semantic_prewarm(
        self,
        file_paths: list[str] | None,
        *,
        task_id: str | None = None,
        project_path: str | None = None,
        reason: str = "",
        limit: int | None = None,
        allow_llm: bool | None = None,
    ) -> dict[str, Any]:
        if not self.config.semantic_auto_generate.enabled:
            return {"queued": False, "reason": "disabled", "module_paths": []}
        resolved_project = str(self._project_config_for(project_path).project_path)
        limit_value = limit if limit is not None else self.config.semantic_auto_generate.max_modules_per_write
        module_paths = self._module_candidates_for_files(
            file_paths,
            task_id=task_id,
            project_path=resolved_project,
            limit=limit_value,
        )
        if not module_paths:
            return {"queued": False, "reason": "no_modules", "module_paths": []}
        payload = json.dumps(
            {"project_path": resolved_project, "module_paths": module_paths, "task_id": task_id, "reason": reason},
            sort_keys=True,
        )
        job_key = f"semantic:{hashlib.sha1(payload.encode('utf-8')).hexdigest()}"
        llm_allowed = self.config.semantic_auto_generate.allow_llm if allow_llm is None else allow_llm
        with self._semantic_jobs_lock:
            active_jobs = sum(1 for future in self._semantic_jobs.values() if not future.done())
            if active_jobs >= max(1, self.config.semantic_auto_generate.max_queue_size):
                return {"queued": False, "reason": "queue_full", "module_paths": module_paths}
            existing = self._semantic_jobs.get(job_key)
            if existing and not existing.done():
                return {"queued": False, "reason": "already_running", "module_paths": module_paths}
            self._semantic_jobs[job_key] = self._semantic_executor.submit(
                self._run_semantic_prewarm,
                job_key,
                resolved_project,
                module_paths,
                task_id,
                limit_value,
                llm_allowed,
            )
        return {"queued": True, "reason": reason or "unspecified", "job_key": job_key, "module_paths": module_paths}

    def _semantic_lookup_suggestions(self, relevant_files: list[str], project_path: str | None = None, limit: int = 6) -> list[dict[str, Any]]:
        store = self._store(project_path)
        suggestions: list[dict[str, Any]] = []
        for file_path in relevant_files:
            module = store.get_module_index(file_path)
            if module:
                suggestions.append(
                    {
                        "entity_key": module["entity_key"],
                        "entity_type": module["entity_type"],
                        "name": module["name"],
                        "file_path": module["file_path"],
                        "summary_hint": module.get("summary_hint", ""),
                    }
                )
        seen = set()
        ordered: list[dict[str, Any]] = []
        for item in suggestions:
            if item["entity_key"] in seen:
                continue
            seen.add(item["entity_key"])
            ordered.append(item)
            if len(ordered) >= limit:
                break
        return ordered

    def _context_scope_key(self, task: dict[str, Any] | None) -> str:
        if task:
            return f"task:{task['id']}"
        return "project:current"

    def _artifact_signature(self, payload: dict[str, Any]) -> str:
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        return hashlib.sha1(encoded.encode("utf-8")).hexdigest()

    def _estimated_tokens(self, text: str) -> int:
        return (len(text.encode("utf-8")) + len(text)) // 4

    def _query_terms(self, query: str) -> list[str]:
        terms = []
        for term in re.split(r"[^A-Za-z0-9_]+", query.lower()):
            if len(term) >= 2 and term not in {"the", "and", "for", "with", "from", "that", "this"}:
                terms.append(term)
        return terms

    def _match_score(self, text: str, terms: list[str]) -> int:
        haystack = text.lower()
        score = 0
        for term in terms:
            if term in haystack:
                score += 1
        return score

    def _rank_values(self, values: list[Any], *, terms: list[str], text_getter: Callable[[Any], str], limit: int) -> list[Any]:
        if not terms:
            return values[:limit]
        ranked = sorted(
            values,
            key=lambda item: (
                -self._match_score(text_getter(item), terms),
                text_getter(item),
            ),
        )
        filtered = [item for item in ranked if self._match_score(text_getter(item), terms) > 0]
        return (filtered or ranked)[:limit]

    def _task_type_for_context(self, task: dict[str, Any] | None) -> str:
        if not task:
            return "general"
        tags = {str(tag).lower() for tag in task.get("tags", [])}
        for candidate in ("bug", "feature", "research", "refactor", "documentation", "testing"):
            if candidate in tags:
                if candidate == "documentation":
                    return "docs"
                if candidate == "testing":
                    return "test"
                return candidate
        title = str(task.get("title", "")).lower()
        if title.startswith("bug:"):
            return "bug"
        if title.startswith("feature:"):
            return "feature"
        if title.startswith("research:"):
            return "research"
        return "general"

    def _apply_section_order_policy(
        self,
        sections: list[dict[str, Any]],
        *,
        task: dict[str, Any] | None,
        mode: str,
    ) -> list[dict[str, Any]]:
        task_type = self._task_type_for_context(task)
        base_order = {
            "header": 0,
            "mission": 10,
            "current_task": 20,
            "relevant_files": 30,
            "latest_handoff": 40,
            "blockers": 50,
            "recent_work": 60,
            "recent_commands": 65,
            "decisions": 70,
            "semantic": 80,
            "sessions": 90,
            "audit": 100,
            "dependencies": 110,
            "active_tasks": 120,
            "daily_notes": 130,
            "stable_header": 0,
            "success_criteria": 12,
            "architecture": 14,
            "working_agreements": 16,
            "atlas_snapshot": 18,
            "dynamic_header": 0,
            "recent_decisions": 70,
        }
        overrides: dict[str, int] = {}
        if task_type == "bug":
            overrides.update({
                "blockers": 25,
                "recent_work": 28,
                "recent_commands": 29,
                "decisions": 30,
                "relevant_files": 30,
                "latest_handoff": 32,
                "semantic": 40,
            })
        elif task_type == "research":
            overrides.update({
                "decisions": 25,
                "semantic": 28,
                "relevant_files": 30,
                "recent_work": 35,
                "recent_commands": 36,
            })
        elif task_type in {"feature", "refactor"}:
            overrides.update({
                "relevant_files": 25,
                "semantic": 28,
                "recent_work": 32,
                "recent_commands": 33,
                "decisions": 35,
            })
        if mode in {"debug", "recovery"}:
            overrides.update({
                "blockers": min(overrides.get("blockers", 50), 24),
                "recent_work": min(overrides.get("recent_work", 60), 26),
                "recent_commands": min(overrides.get("recent_commands", 65), 27),
                "audit": 28,
                "sessions": 29,
            })
        indexed = list(enumerate(sections))
        ordered = sorted(
            indexed,
            key=lambda item: (
                overrides.get(item[1]["name"], base_order.get(item[1]["name"], 200 + item[0])),
                item[0],
            ),
        )
        return [section for _, section in ordered]

    def _split_markdown_sections(self, markdown: str, *, fallback_name: str) -> list[dict[str, Any]]:
        lines = markdown.strip().splitlines()
        if not lines:
            return []
        sections: list[dict[str, Any]] = []
        current_lines: list[str] = []
        current_name = fallback_name
        current_priority = 0
        for line in lines:
            if line.startswith("#"):
                if current_lines:
                    sections.append(
                        {
                            "name": current_name,
                            "layer": "derived",
                            "priority": max(current_priority, 1),
                            "text": "\n".join(current_lines).strip(),
                        }
                    )
                current_lines = [line]
                normalized = re.sub(r"[^a-z0-9]+", "_", line.lstrip("# ").strip().lower()).strip("_")
                current_name = normalized or fallback_name
                current_priority += 1
            else:
                current_lines.append(line)
        if current_lines:
            sections.append(
                {
                    "name": current_name,
                    "layer": "derived",
                    "priority": max(current_priority, 1),
                    "text": "\n".join(current_lines).strip(),
                }
            )
        return sections

    def _record_token_usage_metric(
        self,
        *,
        operation: str,
        project_path: str | None = None,
        event_type: str = "local_estimate",
        actor: str = "obsmcp",
        session_id: str | None = None,
        task_id: str | None = None,
        model_name: str = "",
        provider: str = "",
        client_name: str = "",
        raw_input_tokens: int = 0,
        raw_output_tokens: int = 0,
        estimated_input_tokens: int = 0,
        estimated_output_tokens: int = 0,
        compact_input_tokens: int = 0,
        compact_output_tokens: int = 0,
        saved_tokens: int = 0,
        cache_creation_input_tokens: int = 0,
        cache_read_input_tokens: int = 0,
        raw_chars: int = 0,
        compact_chars: int = 0,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        return self._store(project_path).record_token_usage_event(
            event_type=event_type,
            operation=operation,
            actor=actor,
            session_id=session_id,
            task_id=task_id,
            model_name=model_name,
            provider=provider,
            client_name=client_name,
            raw_input_tokens=raw_input_tokens,
            raw_output_tokens=raw_output_tokens,
            estimated_input_tokens=estimated_input_tokens,
            estimated_output_tokens=estimated_output_tokens,
            compact_input_tokens=compact_input_tokens,
            compact_output_tokens=compact_output_tokens,
            saved_tokens=saved_tokens,
            cache_creation_input_tokens=cache_creation_input_tokens,
            cache_read_input_tokens=cache_read_input_tokens,
            raw_chars=raw_chars,
            compact_chars=compact_chars,
            metadata=metadata,
        )

    def _strip_ansi(self, text: str) -> str:
        return re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", text)

    def _dedupe_lines(self, lines: list[str], *, max_repeats: int = 1) -> list[str]:
        deduped: list[str] = []
        last_line: str | None = None
        repeat_count = 0
        for line in lines:
            if line == last_line:
                repeat_count += 1
                if repeat_count >= max_repeats:
                    continue
            else:
                repeat_count = 0
            deduped.append(line)
            last_line = line
        return deduped

    def _limit_head_tail(self, lines: list[str], *, head: int, tail: int) -> list[str]:
        if len(lines) <= head + tail:
            return lines
        omitted = len(lines) - head - tail
        return lines[:head] + [f"... ({omitted} lines omitted) ..."] + lines[-tail:]

    def _focus_lines(self, lines: list[str], keywords: list[str], *, limit: int) -> list[str]:
        matches: list[str] = []
        lowered = [keyword.lower() for keyword in keywords]
        for line in lines:
            candidate = line.lower()
            if any(keyword in candidate for keyword in lowered):
                matches.append(line)
                if len(matches) >= limit:
                    break
        return matches

    def _classify_tool_output_profile(self, command: str) -> str:
        normalized = " ".join(command.lower().split())
        if normalized.startswith("git status"):
            return "git_status"
        if normalized.startswith("git diff"):
            return "git_diff"
        if normalized.startswith("rg ") or normalized.startswith("ripgrep ") or " grep " in f" {normalized} ":
            return "search"
        if normalized.startswith("cat ") or normalized.startswith("type ") or normalized.startswith("get-content"):
            return "read"
        if any(token in normalized for token in ("pytest", "unittest", "npm test", "cargo test", "go test")):
            return "tests"
        if any(token in normalized for token in ("npm run build", "npm run lint", "eslint", "ruff", "mypy", "cargo build", "tsc", "make ")):
            return "build"
        if any(token in normalized for token in ("docker logs", "journalctl", " tail", "logcat", "logs")):
            return "logs"
        return "generic"

    def _compact_generic_output(
        self,
        lines: list[str],
        *,
        head: int,
        tail: int,
        focus_keywords: list[str] | None = None,
        focus_limit: int = 24,
    ) -> tuple[list[str], dict[str, Any]]:
        normalized = self._dedupe_lines([line.rstrip() for line in lines], max_repeats=1)
        non_empty = [line for line in normalized if line.strip()]
        focus: list[str] = []
        if focus_keywords:
            focus = self._focus_lines(non_empty, focus_keywords, limit=focus_limit)
        focus_set = set(focus)
        remaining = [line for line in non_empty if line not in focus_set]
        selected: list[str] = []
        if focus:
            selected.append("[focused lines]")
            selected.extend(focus)
            selected.append("")
        selected.extend(self._limit_head_tail(remaining, head=head, tail=tail))
        return selected, {
            "total_lines": len(lines),
            "normalized_lines": len(non_empty),
            "focused_lines": len(focus),
        }

    def _compact_git_status_output(self, lines: list[str]) -> tuple[list[str], dict[str, Any]]:
        normalized = [line.rstrip() for line in self._dedupe_lines(lines, max_repeats=1) if line.strip()]
        summary_lines = [
            line
            for line in normalized
            if line.startswith("On branch")
            or line.startswith("HEAD detached")
            or line.startswith("Your branch")
            or line.startswith("nothing to commit")
            or line.startswith("Changes to be committed")
            or line.startswith("Changes not staged")
            or line.startswith("Untracked files")
        ]
        file_lines = [
            line.strip()
            for line in normalized
            if ":" in line or line.lstrip().startswith("?? ") or line.startswith("\t")
        ]
        selected = summary_lines[:8]
        if file_lines:
            selected.append("")
            selected.append("Files:")
            selected.extend(f"- {line.lstrip('?').strip()}" for line in file_lines[:20])
            if len(file_lines) > 20:
                selected.append(f"- ... {len(file_lines) - 20} more file entries omitted")
        return selected or self._limit_head_tail(normalized, head=20, tail=10), {
            "total_lines": len(lines),
            "file_entries": len(file_lines),
        }

    def _compact_git_diff_output(self, lines: list[str]) -> tuple[list[str], dict[str, Any]]:
        normalized = [line.rstrip() for line in self._dedupe_lines(lines, max_repeats=1) if line.strip()]
        file_headers = [line for line in normalized if line.startswith("diff --git")]
        hunks = [line for line in normalized if line.startswith("@@")]
        changes = [line for line in normalized if (line.startswith("+") or line.startswith("-")) and not line.startswith("+++") and not line.startswith("---")]
        selected = file_headers[:12] + hunks[:16]
        if changes:
            if selected:
                selected.append("")
                selected.append("Change preview:")
            selected.extend(changes[:24])
            if len(changes) > 24:
                selected.append(f"... {len(changes) - 24} diff lines omitted ...")
        if not selected:
            selected = self._limit_head_tail(normalized, head=25, tail=15)
        return selected, {
            "total_lines": len(lines),
            "files": len(file_headers),
            "hunks": len(hunks),
            "change_lines": len(changes),
        }

    def _compact_search_output(self, lines: list[str]) -> tuple[list[str], dict[str, Any]]:
        normalized = [line.rstrip() for line in self._dedupe_lines(lines, max_repeats=1) if line.strip()]
        selected = normalized[:60]
        if len(normalized) > 60:
            selected.append(f"... {len(normalized) - 60} search results omitted ...")
        return selected, {"total_lines": len(lines), "matches": len(normalized)}

    def _compact_read_output(self, lines: list[str]) -> tuple[list[str], dict[str, Any]]:
        normalized = [line.rstrip() for line in lines]
        selected = self._limit_head_tail(normalized, head=50, tail=20)
        return selected, {"total_lines": len(lines)}

    def _compact_test_or_log_output(self, lines: list[str], *, exit_code: int, profile: str) -> tuple[list[str], dict[str, Any]]:
        focus_keywords = ["fail", "failed", "error", "exception", "traceback", "assert", "panic"]
        if profile == "logs":
            focus_keywords.extend(["warn", "timeout", "critical"])
        selected, info = self._compact_generic_output(
            lines,
            head=18,
            tail=28 if exit_code else 18,
            focus_keywords=focus_keywords,
            focus_limit=32,
        )
        return selected, info

    def _build_optimization_policy(
        self,
        *,
        mode: str,
        task: dict[str, Any] | None,
        command_profile: str | None = None,
        exit_code: int = 0,
    ) -> dict[str, Any]:
        normalized_mode = mode.lower()
        presets: dict[str, dict[str, Any]] = {
            "compact": {
                "mode": "compact",
                "window_scale": 0.7,
                "focus_limit": 16,
                "raw_capture_on_failure": True,
                "raw_capture_on_truncation": False,
                "chunk_count_hint": 2,
                "stable_ratio": 0.6,
                "context_mode": "compact",
            },
            "balanced": {
                "mode": "balanced",
                "window_scale": 1.0,
                "focus_limit": 24,
                "raw_capture_on_failure": True,
                "raw_capture_on_truncation": True,
                "chunk_count_hint": 3,
                "stable_ratio": 0.5,
                "context_mode": "balanced",
            },
            "debug": {
                "mode": "debug",
                "window_scale": 1.5,
                "focus_limit": 40,
                "raw_capture_on_failure": True,
                "raw_capture_on_truncation": True,
                "chunk_count_hint": 4,
                "stable_ratio": 0.45,
                "context_mode": "debug",
            },
            "recovery": {
                "mode": "recovery",
                "window_scale": 1.3,
                "focus_limit": 32,
                "raw_capture_on_failure": True,
                "raw_capture_on_truncation": True,
                "chunk_count_hint": 4,
                "stable_ratio": 0.45,
                "context_mode": "recovery",
            },
        }
        policy = dict(presets.get(normalized_mode, presets["balanced"]))
        task_type = self._task_type_for_context(task)
        if task_type == "bug":
            policy["window_scale"] = max(policy["window_scale"], 1.1)
            policy["focus_limit"] = max(policy["focus_limit"], 28)
        elif task_type == "research":
            policy["stable_ratio"] = min(max(policy["stable_ratio"], 0.55), 0.7)
        if command_profile in {"tests", "build", "logs"} and exit_code != 0:
            policy["window_scale"] = max(policy["window_scale"], 1.4)
            policy["focus_limit"] = max(policy["focus_limit"], 36)
        policy["task_type"] = task_type
        policy["command_profile"] = command_profile
        policy["exit_code"] = exit_code
        return policy

    def get_optimization_policy(
        self,
        mode: str = "balanced",
        task_id: str | None = None,
        command: str | None = None,
        exit_code: int = 0,
        project_path: str | None = None,
    ) -> dict[str, Any]:
        store = self._store(project_path)
        task = store.get_task(task_id) if task_id else store.get_current_task()
        command_profile = self._classify_tool_output_profile(command or "") if command else None
        policy = self._build_optimization_policy(
            mode=mode,
            task=task,
            command_profile=command_profile,
            exit_code=exit_code,
        )
        policy["task_id"] = (task or {}).get("id")
        policy["project_path"] = str(self._project_config_for(project_path).project_path)
        return policy

    def _classify_command_execution_risk(self, command: str) -> dict[str, Any]:
        normalized = (command or "").strip().lower()
        read_prefixes = ("rg ", "ripgrep ", "grep ", "ls", "dir", "pwd", "cat ", "type ", "git status", "git diff", "git show", "find ", "where ", "which ")
        write_markers = ("write", "apply_patch", "touch ", "echo ", "tee ", "sed -i", "python ", "npm run build", "pytest", "unittest", "cargo test", "make test")
        install_markers = ("npm install", "pip install", "uv sync", "poetry install", "cargo add", "apt ", "brew ", "choco ")
        destructive_markers = ("rm ", "del ", "remove-item", "git reset", "git checkout --", "format ", "drop ", "truncate ", "shutdown", "reboot")
        network_markers = ("curl ", "wget ", "Invoke-WebRequest".lower(), "git clone", "git fetch", "git pull", "npm publish")

        action_type = "unknown"
        risk_level = "medium"
        if any(normalized.startswith(prefix) for prefix in read_prefixes):
            action_type = "read"
            risk_level = "low"
        elif any(marker in normalized for marker in destructive_markers):
            action_type = "destructive"
            risk_level = "high"
        elif any(marker in normalized for marker in install_markers):
            action_type = "install"
            risk_level = "high"
        elif any(marker in normalized for marker in network_markers):
            action_type = "network"
            risk_level = "high"
        elif any(marker in normalized for marker in write_markers):
            action_type = "write"
            risk_level = "medium"

        can_batch = action_type in {"read"} and risk_level == "low"
        needs_model_review = risk_level == "high" or action_type in {"destructive", "install", "network"}
        return {
            "command": command,
            "action_type": action_type,
            "risk_level": risk_level,
            "can_batch": can_batch,
            "needs_model_review": needs_model_review,
            "recommended_sync_mode": "deferred" if action_type in {"read", "write"} else "full",
        }

    def get_command_execution_policy(
        self,
        command: str,
        task_id: str | None = None,
        mode: str = "balanced",
        exit_code: int = 0,
        project_path: str | None = None,
    ) -> dict[str, Any]:
        base = self._classify_command_execution_risk(command)
        optimization = self.get_optimization_policy(
            mode=mode,
            task_id=task_id,
            command=command,
            exit_code=exit_code,
            project_path=project_path,
        )
        output_policy = self.get_output_response_policy(
            task_id=task_id,
            operation_kind="dangerous_actions" if base["action_type"] == "destructive" else "general",
            command=command,
            project_path=project_path,
        )
        return {
            **base,
            "mode": mode,
            "task_id": optimization.get("task_id"),
            "project_path": optimization.get("project_path"),
            "optimization_policy": optimization,
            "output_policy": output_policy,
        }

    def _resolve_output_policy(
        self,
        task_id: str | None = None,
        *,
        operation_kind: str = "general",
        detail_requested: bool = False,
        command: str | None = None,
        project_path: str | None = None,
    ) -> EffectiveOutputPolicy:
        store = self._store(project_path)
        task = store.get_task(task_id) if task_id else store.get_current_task()
        return resolve_output_policy(
            self.config.output_compression,
            task=task,
            operation_kind=operation_kind,
            detail_requested=detail_requested,
            command=command,
        )

    def get_output_response_policy(
        self,
        task_id: str | None = None,
        operation_kind: str = "general",
        detail_requested: bool = False,
        command: str | None = None,
        project_path: str | None = None,
    ) -> dict[str, Any]:
        policy = self._resolve_output_policy(
            task_id=task_id,
            operation_kind=operation_kind,
            detail_requested=detail_requested,
            command=command,
            project_path=project_path,
        )
        return policy.to_dict()

    def _record_output_policy_metric(
        self,
        *,
        operation: str,
        policy: EffectiveOutputPolicy,
        rendered_text: str,
        task_id: str | None = None,
        project_path: str | None = None,
    ) -> None:
        observability = self.config.output_compression.observability
        if not observability.log_metrics or observability.sample_rate <= 0:
            return
        metadata: dict[str, Any] = {
            "detail_requested": policy.detail_requested,
            "bypassed": policy.bypassed,
            "bypass_reason": policy.bypass_reason,
            "operation_kind": policy.operation_kind,
        }
        if observability.record_mode:
            metadata["output_mode"] = policy.mode
        if observability.record_style:
            metadata["output_style"] = policy.style
            metadata["output_level"] = policy.level
        if observability.record_task_type:
            metadata["task_type"] = policy.task_type
        self._record_token_usage_metric(
            operation=operation,
            project_path=project_path,
            event_type="output_policy",
            task_id=task_id,
            estimated_output_tokens=self._estimated_tokens(rendered_text),
            compact_output_tokens=self._estimated_tokens(rendered_text),
            compact_chars=len(rendered_text),
            metadata=metadata,
        )

    def _compact_tool_output_lines(
        self,
        *,
        profile: str,
        output: str,
        exit_code: int,
        policy: dict[str, Any] | None = None,
    ) -> tuple[str, dict[str, Any]]:
        stripped = self._strip_ansi(output)
        lines = stripped.splitlines()
        scale = float((policy or {}).get("window_scale", 1.0))
        focus_limit = int((policy or {}).get("focus_limit", 24))
        def scaled(value: int) -> int:
            return max(4, int(round(value * scale)))
        if profile == "git_status":
            selected_lines, profile_info = self._compact_git_status_output(lines)
        elif profile == "git_diff":
            selected_lines, profile_info = self._compact_git_diff_output(lines)
        elif profile == "search":
            selected_lines, profile_info = self._compact_search_output(lines[: max(scaled(60), 20)])
        elif profile == "read":
            selected_lines, profile_info = self._compact_generic_output(
                lines,
                head=scaled(50),
                tail=scaled(20),
                focus_keywords=["todo", "fixme", "error", "warning"] if exit_code else None,
                focus_limit=focus_limit,
            )
        elif profile in {"tests", "build", "logs"}:
            focus_keywords = ["fail", "failed", "error", "exception", "traceback", "assert", "panic"]
            if profile == "logs":
                focus_keywords.extend(["warn", "timeout", "critical"])
            selected_lines, profile_info = self._compact_generic_output(
                lines,
                head=scaled(18),
                tail=scaled(28 if exit_code else 18),
                focus_keywords=focus_keywords,
                focus_limit=focus_limit,
            )
        else:
            focus_keywords = ["error", "exception", "fail", "warning"] if exit_code else ["error", "warning"]
            selected_lines, profile_info = self._compact_generic_output(
                lines,
                head=scaled(20),
                tail=scaled(20),
                focus_keywords=focus_keywords,
                focus_limit=focus_limit,
            )
        compact_text = "\n".join(line for line in selected_lines if line is not None).strip()
        if not compact_text:
            compact_text = stripped.strip()
        return compact_text + ("\n" if compact_text else ""), profile_info

    def _store_raw_output_capture(
        self,
        *,
        command: str,
        output: str,
        profile: str,
        reason: str,
        project_path: str | None = None,
        actor: str = "obsmcp",
        session_id: str | None = None,
        task_id: str | None = None,
        exit_code: int = 0,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        pcfg = self._project_config_for(project_path)
        raw_dir = pcfg.export_dir / "raw-output"
        raw_dir.mkdir(parents=True, exist_ok=True)
        capture_id = f"RAW-{uuid.uuid4().hex[:10].upper()}"
        capture_path = raw_dir / f"{capture_id}.log"
        write_text_atomic(capture_path, output)
        preview = self._strip_ansi(output).strip()[:400]
        return self._store(project_path).create_raw_output_capture(
            capture_id=capture_id,
            actor=actor,
            command_text=command,
            profile=profile,
            reason=reason,
            output_path=str(capture_path),
            preview=preview,
            session_id=session_id,
            task_id=task_id,
            exit_code=exit_code,
            raw_chars=len(output),
            raw_tokens_est=self._estimated_tokens(output),
            metadata=metadata,
        )

    def _summarize_command_stream(
        self,
        *,
        output: str,
        profile: str,
        exit_code: int,
        policy: dict[str, Any],
    ) -> dict[str, Any]:
        stripped = self._strip_ansi(output).strip()
        if not stripped:
            return {
                "summary": "",
                "raw_chars": 0,
                "compact_chars": 0,
                "raw_tokens_est": 0,
                "compact_tokens_est": 0,
                "raw_lines": 0,
                "compact_lines": 0,
                "was_compacted": False,
                "profile_info": {"total_lines": 0},
            }
        compact_output, profile_info = self._compact_tool_output_lines(
            profile=profile,
            output=output,
            exit_code=exit_code,
            policy=policy,
        )
        raw_text = stripped + "\n"
        compact_text = compact_output if compact_output.endswith("\n") or not compact_output else compact_output + "\n"
        return {
            "summary": compact_text,
            "raw_chars": len(output),
            "compact_chars": len(compact_text),
            "raw_tokens_est": self._estimated_tokens(output),
            "compact_tokens_est": self._estimated_tokens(compact_text),
            "raw_lines": len(raw_text.splitlines()),
            "compact_lines": len(compact_text.splitlines()),
            "was_compacted": compact_text.strip() != raw_text.strip(),
            "profile_info": profile_info,
        }

    def _single_line_summary(self, text: str, max_chars: int = 220) -> str:
        collapsed = " ".join(part.strip() for part in self._strip_ansi(text).splitlines() if part.strip())
        if len(collapsed) <= max_chars:
            return collapsed
        return collapsed[: max_chars - 3].rstrip() + "..."

    def _build_command_event_summary(
        self,
        *,
        command_text: str,
        status: str,
        exit_code: int,
        duration_ms: int,
        stdout_summary: str,
        stderr_summary: str,
        raw_capture: dict[str, Any] | None,
    ) -> str:
        duration_note = f" in {duration_ms} ms" if duration_ms > 0 else ""
        normalized_status = status or ("failed" if exit_code else "completed")
        headline = f"{normalized_status}: `{command_text}`"
        if exit_code:
            headline += f" exited with code {exit_code}"
        headline += duration_note
        details = self._single_line_summary(stderr_summary or stdout_summary)
        if not details:
            details = "No output summary recorded."
        if raw_capture:
            details += f" Raw output saved as {raw_capture['capture_id']}."
        return f"{headline}. {details}".strip()

    def _render_context_sections(
        self,
        sections: list[dict[str, Any]],
        max_tokens: int | None = None,
        chunk_index: int = 0,
        total_chunks: int = 1,
    ) -> tuple[str, int]:
        """Render context sections with optional chunking.

        When total_chunks > 1, sections are divided across chunks by priority order.
        Chunk boundaries are kept at section boundaries to avoid mid-section splits.
        """
        if total_chunks > 1:
            chunks = self._chunk_sections(sections, total_chunks)
            target = chunks[chunk_index] if chunk_index < len(chunks) else []
        else:
            target = sections

        rendered_parts: list[str] = []
        used_tokens = 0
        budget = max_tokens if max_tokens and max_tokens > 0 else None
        for section in target:
            text = section["text"].rstrip()
            if not text:
                continue
            tokens = self._estimated_tokens(text)
            if budget is None or used_tokens + tokens <= budget:
                rendered_parts.append(text)
                used_tokens += tokens
                continue
            remaining = budget - used_tokens
            if remaining <= 0:
                break
            char_budget = max(remaining * 4, 40)
            truncated = text[:char_budget].rsplit("\n", 1)[0].rstrip()
            if truncated:
                rendered_parts.append(truncated + "\n\n_(truncated due to token budget)_")
                used_tokens = budget
            break
        markdown = "\n\n".join(part for part in rendered_parts if part).strip() + "\n"
        return markdown, used_tokens

    def _chunk_sections(
        self,
        sections: list[dict[str, Any]],
        total_chunks: int,
    ) -> list[list[dict[str, Any]]]:
        """Split sections into total_chunks roughly equal chunks, respecting section boundaries."""
        if total_chunks <= 1:
            return [sections]
        total_priority = sum(s["priority"] for s in sections) or 1
        chunks: list[list[dict[str, Any]]] = [[] for _ in range(total_chunks)]
        chunk_weights = [total_priority / total_chunks] * total_chunks
        current_weights = [0.0] * total_chunks
        chunk_idx = 0
        for section in sections:
            chunks[chunk_idx].append(section)
            current_weights[chunk_idx] += section["priority"]
            if chunk_idx < total_chunks - 1 and current_weights[chunk_idx] >= chunk_weights[chunk_idx]:
                chunk_idx += 1
        return [c for c in chunks if c]

    def _collect_context_data(
        self,
        task_id: str | None = None,
        project_path: str | None = None,
        *,
        recent_work_limit: int = 6,
        decisions_limit: int = 5,
        blockers_limit: int = 6,
        relevant_files_limit: int = 12,
        semantic_limit: int = 6,
        active_sessions_limit: int = 5,
        active_tasks_limit: int = 10,
        include_dependency_map: bool = False,
        include_daily_notes: bool = False,
        include_session_info: bool = True,
        include_semantic: bool = True,
    ) -> dict[str, Any]:
        store = self._store(project_path)
        brief = store.get_project_brief()
        task = store.get_task(task_id) if task_id else store.get_current_task()
        relevant_files = store.get_relevant_files(task_id=task["id"] if task else task_id, limit=relevant_files_limit) if (task or task_id) else store.get_relevant_files(limit=relevant_files_limit)
        data = {
            "brief": brief,
            "task": task,
            "blockers": store.get_blockers(open_only=True, limit=blockers_limit),
            "decisions": store.get_decisions(limit=decisions_limit),
            "recent_work": store.get_recent_work(limit=recent_work_limit),
            "recent_commands": store.list_command_events(limit=6, task_id=task["id"] if task else task_id),
            "relevant_files": relevant_files,
            "latest_handoff": store.get_latest_handoff(),
            "active_sessions": store.get_active_sessions(limit=active_sessions_limit) if include_session_info else [],
            "active_tasks": store.get_active_tasks(limit=active_tasks_limit),
            "audit": store.detect_missing_writeback(),
            "daily_notes": store.get_daily_entries(limit=5) if include_daily_notes else [],
            "dependencies": store.get_all_dependencies() if include_dependency_map else [],
            "blocked_tasks": store.get_blocked_tasks() if include_dependency_map else [],
            "semantic_suggestions": self._semantic_lookup_suggestions(relevant_files, project_path=project_path, limit=semantic_limit) if include_semantic else [],
        }
        return data

    def _build_tiered_sections(
        self,
        profile: str,
        *,
        task_id: str | None = None,
        project_path: str | None = None,
        include_daily_notes: bool = False,
        include_dependency_map: bool | None = None,
        include_session_info: bool | None = None,
        include_recent_work: bool = True,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        normalized_profile = profile.lower()
        dependency_default = normalized_profile in {"deep", "handoff", "recovery"}
        session_default = normalized_profile != "fast"
        data = self._collect_context_data(
            task_id=task_id,
            project_path=project_path,
            recent_work_limit=10 if normalized_profile in {"deep", "recovery"} else 6,
            decisions_limit=8 if normalized_profile in {"deep", "handoff", "recovery"} else 4,
            blockers_limit=8 if normalized_profile in {"deep", "handoff", "recovery"} else 5,
            relevant_files_limit=16 if normalized_profile in {"deep", "handoff", "recovery"} else 10,
            semantic_limit=8 if normalized_profile in {"deep", "handoff", "recovery"} else 5,
            active_sessions_limit=6,
            active_tasks_limit=12,
            include_dependency_map=dependency_default if include_dependency_map is None else include_dependency_map,
            include_daily_notes=include_daily_notes,
            include_session_info=session_default if include_session_info is None else include_session_info,
            include_semantic=True,
        )
        task = data["task"]
        sections: list[dict[str, Any]] = []
        sections.append(
            {
                "name": "header",
                "layer": "L0",
                "priority": 0,
                "text": "\n".join(
                    [
                        f"# {normalized_profile.title()} Context",
                        f"Generated: {utc_now()}",
                        f"Project: {self._project_config_for(project_path).project_name}",
                    ]
                ),
            }
        )

        mission = data["brief"].get("Mission", "").strip() or "No mission recorded yet."
        current_task_lines = ["## Current Task"]
        if task:
            current_task_lines.extend(
                [
                    f"- ID: {task['id']}",
                    f"- Title: {task['title']}",
                    f"- Status: {task['status']}",
                    f"- Priority: {task['priority']}",
                    "",
                    task["description"] or "No description.",
                ]
            )
        else:
            current_task_lines.append("- No current task is set.")
        sections.extend(
            [
                {"name": "mission", "layer": "L0", "priority": 1, "text": f"## Mission\n{mission}"},
                {"name": "current_task", "layer": "L0", "priority": 2, "text": "\n".join(current_task_lines)},
            ]
        )

        relevant_lines = ["## Relevant Files"]
        relevant_lines.extend([f"- {item}" for item in data["relevant_files"]] or ["- None"])
        sections.append({"name": "relevant_files", "layer": "L0", "priority": 3, "text": "\n".join(relevant_lines)})

        handoff = data["latest_handoff"]
        handoff_lines = ["## Latest Handoff"]
        if handoff:
            handoff_lines.extend(
                [
                    f"- From: {handoff['from_actor']}",
                    f"- To: {handoff['to_actor']}",
                    f"- Created: {handoff['created_at']}",
                    "",
                    handoff["summary"] or "No summary.",
                    "",
                    f"Next Steps: {handoff['next_steps'] or 'None recorded.'}",
                ]
            )
        else:
            handoff_lines.append("- No handoff recorded yet.")
        sections.append({"name": "latest_handoff", "layer": "L0", "priority": 4, "text": "\n".join(handoff_lines)})

        blocker_lines = ["## Open Blockers"]
        blocker_lines.extend([f"- [{item['id']}] {item['title']}: {item['description']}" for item in data["blockers"]] or ["- None"])
        sections.append({"name": "blockers", "layer": "L0", "priority": 5, "text": "\n".join(blocker_lines)})

        if include_recent_work:
            work_lines = ["## Recent Work"]
            work_lines.extend([f"- {item['created_at']} [{item['actor']}] {item['message']}" for item in data["recent_work"]] or ["- None"])
            sections.append({"name": "recent_work", "layer": "L1", "priority": 6, "text": "\n".join(work_lines)})

        command_lines = ["## Recent Commands"]
        command_lines.extend(
            [
                f"- {item['created_at']} [{item['status']}] `{item['command_text']}`"
                + (f" -> {item['summary']}" if item.get("summary") else "")
                for item in data["recent_commands"]
            ]
            or ["- None"]
        )
        sections.append({"name": "recent_commands", "layer": "L1", "priority": 7, "text": "\n".join(command_lines)})

        decision_lines = ["## Recent Decisions"]
        decision_lines.extend([f"- [{item['id']}] {item['title']}: {item['decision']}" for item in data["decisions"]] or ["- None"])
        sections.append({"name": "decisions", "layer": "L1", "priority": 8, "text": "\n".join(decision_lines)})

        semantic_lines = ["## Recommended Semantic Lookups"]
        semantic_lines.extend([f"- {item['entity_key']}: {item['summary_hint'] or item['name']}" for item in data["semantic_suggestions"]] or ["- None"])
        sections.append({"name": "semantic", "layer": "L2", "priority": 9, "text": "\n".join(semantic_lines)})

        if data["active_sessions"]:
            session_lines = ["## Active Sessions"]
            session_lines.extend(
                [
                    f"- {item['id']} [{item['status']}] {item['actor']} ({item['client_name']}/{item['model_name']})"
                    for item in data["active_sessions"]
                ]
            )
            sections.append({"name": "sessions", "layer": "L1", "priority": 10, "text": "\n".join(session_lines)})

        if data["audit"] and normalized_profile in {"handoff", "recovery", "deep"}:
            audit_lines = ["## Session Audit"]
            audit_lines.extend([f"- {item['issue']}: {item['details']}" for item in data["audit"]])
            sections.append({"name": "audit", "layer": "L1", "priority": 11, "text": "\n".join(audit_lines)})

        if data["dependencies"]:
            dep_lines = ["## Dependency Map"]
            for dep in data["dependencies"][:10]:
                blocked_by = ", ".join(dep.get("blocked_by", [])) or "none"
                blocks = ", ".join(dep.get("blocks", [])) or "none"
                dep_lines.append(f"- {dep['task_id']} | blocked_by: {blocked_by} | blocks: {blocks}")
            if data["blocked_tasks"]:
                dep_lines.append("")
                dep_lines.append("Blocked tasks:")
                dep_lines.extend([f"- {task['id']}: {task['title']}" for task in data["blocked_tasks"][:6]])
            sections.append({"name": "dependencies", "layer": "L2", "priority": 12, "text": "\n".join(dep_lines)})

        if data["active_tasks"] and normalized_profile in {"deep", "recovery"}:
            task_lines = ["## Active Tasks Summary"]
            task_lines.extend([f"- {item['id']} [{item['status']}] {item['title']}" for item in data["active_tasks"][:10]])
            sections.append({"name": "active_tasks", "layer": "L3", "priority": 13, "text": "\n".join(task_lines)})

        if data["daily_notes"]:
            note_lines = ["## Daily Notes"]
            note_lines.extend([f"- {item['note_date']} [{item['actor']}] {item['entry']}" for item in data["daily_notes"]])
            sections.append({"name": "daily_notes", "layer": "L3", "priority": 14, "text": "\n".join(note_lines)})

        sections = self._apply_section_order_policy(sections, task=task, mode=normalized_profile)
        metadata = {
            "profile": normalized_profile,
            "task_id": task["id"] if task else None,
            "relevant_files": data["relevant_files"],
            "semantic_entity_keys": [item["entity_key"] for item in data["semantic_suggestions"]],
            "layers": {section["name"]: section["layer"] for section in sections},
        }
        return sections, metadata

    def generate_context_profile(
        self,
        profile: str = "balanced",
        task_id: str | None = None,
        max_tokens: int | None = None,
        include_daily_notes: bool = False,
        project_path: str | None = None,
    ) -> dict[str, Any]:
        store = self._store(project_path)
        task = store.get_task(task_id) if task_id else store.get_current_task()
        params = {
            "profile": profile,
            "task_id": task["id"] if task else task_id,
            "max_tokens": max_tokens,
            "include_daily_notes": include_daily_notes,
        }
        params_signature = self._artifact_signature(params)
        scope_key = self._context_scope_key(task)
        state_version = store.get_context_state_version(include_semantic=True)
        cached = store.get_context_artifact("context_profile", scope_key, params_signature)
        if cached and cached["state_version"] == state_version and not store.is_context_stale(cached):
            metadata = dict(cached.get("metadata", {}))
            metadata["cached"] = True
            result = {
                "profile": profile,
                "scope_key": scope_key,
                "markdown": cached["content"],
                "cached": True,
                "state_version": state_version,
                "metadata": metadata,
            }
            self._record_token_usage_metric(
                operation="generate_context_profile",
                project_path=project_path,
                estimated_output_tokens=int(metadata.get("used_tokens") or self._estimated_tokens(cached["content"])),
                compact_output_tokens=int(metadata.get("used_tokens") or self._estimated_tokens(cached["content"])),
                compact_chars=len(cached["content"]),
                metadata={
                    "profile": profile,
                    "scope_key": scope_key,
                    "cached": True,
                    "state_version": state_version,
                    "sections": metadata.get("section_order", []),
                },
            )
            return result

        sections, metadata = self._build_tiered_sections(
            profile,
            task_id=task["id"] if task else task_id,
            project_path=project_path,
            include_daily_notes=include_daily_notes,
        )
        markdown, used_tokens = self._render_context_sections(sections, max_tokens=max_tokens)
        metadata.update(
            {
                "used_tokens": used_tokens,
                "section_order": [section["name"] for section in sections],
                "max_tokens": max_tokens,
            }
        )
        fingerprints = {fp["file_path"]: fp["fingerprint"] for fp in store.get_file_fingerprints().values()}
        metadata["fingerprint_set"] = fingerprints
        artifact = store.upsert_context_artifact(
            artifact_key=f"context_profile:{scope_key}:{params_signature}",
            artifact_type="context_profile",
            scope_key=scope_key,
            params_signature=params_signature,
            state_version=state_version,
            content=markdown,
            metadata=metadata,
        )
        result = {
            "profile": profile,
            "scope_key": scope_key,
            "markdown": markdown,
            "cached": False,
            "state_version": state_version,
            "metadata": (artifact or {}).get("metadata", metadata),
        }
        self._record_token_usage_metric(
            operation="generate_context_profile",
            project_path=project_path,
            estimated_output_tokens=used_tokens,
            compact_output_tokens=used_tokens,
            compact_chars=len(markdown),
            metadata={
                "profile": profile,
                "scope_key": scope_key,
                "cached": False,
                "state_version": state_version,
                "sections": metadata.get("section_order", []),
            },
        )
        return result

    def _resolve_delta_reference(
        self,
        *,
        store: StateStore,
        since_handoff_id: int | None = None,
        since_session_id: str | None = None,
        since_timestamp: str | None = None,
    ) -> tuple[str, str]:
        if since_timestamp:
            return since_timestamp, "timestamp"
        if since_handoff_id is not None:
            handoff = store.get_handoff(since_handoff_id)
            if not handoff:
                raise ValueError(f"Unknown handoff id: {since_handoff_id}")
            return handoff["created_at"], "handoff"
        if since_session_id:
            session = store.get_session(since_session_id)
            if not session:
                raise ValueError(f"Unknown session id: {since_session_id}")
            return session.get("opened_at") or session.get("heartbeat_at") or utc_now(), "session"
        latest_handoff = store.get_latest_handoff()
        if latest_handoff:
            return latest_handoff["created_at"], "latest_handoff"
        sessions = store.get_active_sessions(limit=1)
        if sessions:
            session = sessions[0]
            return session.get("opened_at") or session.get("heartbeat_at") or utc_now(), "active_session"
        return utc_now(), "now"

    def generate_delta_context(
        self,
        task_id: str | None = None,
        since_handoff_id: int | None = None,
        since_session_id: str | None = None,
        since_timestamp: str | None = None,
        project_path: str | None = None,
    ) -> dict[str, Any]:
        store = self._store(project_path)
        task = store.get_task(task_id) if task_id else store.get_current_task()
        params = {
            "task_id": task["id"] if task else task_id,
            "since_handoff_id": since_handoff_id,
            "since_session_id": since_session_id,
            "since_timestamp": since_timestamp,
        }
        params_signature = self._artifact_signature(params)
        scope_key = self._context_scope_key(task)
        state_version = store.get_context_state_version(include_semantic=True)
        cached = store.get_context_artifact("delta_context", scope_key, params_signature)
        if cached and cached["state_version"] == state_version and not store.is_context_stale(cached):
            metadata = dict(cached.get("metadata", {}))
            metadata["cached"] = True
            result = {
                "cached": True,
                "markdown": cached["content"],
                "state_version": state_version,
                "metadata": metadata,
            }
            self._record_token_usage_metric(
                operation="generate_delta_context",
                project_path=project_path,
                estimated_output_tokens=self._estimated_tokens(cached["content"]),
                compact_output_tokens=self._estimated_tokens(cached["content"]),
                compact_chars=len(cached["content"]),
                metadata={
                    "cached": True,
                    "scope_key": scope_key,
                    "reference_kind": metadata.get("reference_kind"),
                    "state_version": state_version,
                },
            )
            return result

        reference_time, reference_kind = self._resolve_delta_reference(
            store=store,
            since_handoff_id=since_handoff_id,
            since_session_id=since_session_id,
            since_timestamp=since_timestamp,
        )
        recent_work = store.get_recent_work_since(reference_time, limit=12)
        command_events = store.get_command_events_since(reference_time, limit=12)
        decisions = store.get_decisions_since(reference_time, limit=10)
        blockers = store.get_blockers_since(reference_time, limit=10)
        handoffs = store.get_handoffs_since(reference_time, limit=6)
        tasks = store.get_tasks_updated_since(reference_time, limit=10)
        file_candidates: list[str] = []
        for row in recent_work:
            for file_path in row.get("files", []):
                if file_path not in file_candidates:
                    file_candidates.append(file_path)
        for row in tasks:
            for file_path in row.get("relevant_files", []):
                if file_path not in file_candidates:
                    file_candidates.append(file_path)
        for row in command_events:
            for file_path in row.get("files_changed", []):
                if file_path not in file_candidates:
                    file_candidates.append(file_path)
        semantic = self._semantic_lookup_suggestions(file_candidates, project_path=project_path, limit=6)
        lines = [
            "# Delta Context",
            "",
            f"- Reference Kind: {reference_kind}",
            f"- Since: {reference_time}",
            f"- Task: {(task or {}).get('id', 'none')}",
            "",
            "## Changed Tasks",
            "",
        ]
        lines.extend([f"- {item['id']} [{item['status']}] {item['title']}" for item in tasks] or ["- None"])
        lines.extend(["", "## New Work", ""])
        lines.extend([f"- {item['created_at']} [{item['actor']}] {item['message']}" for item in recent_work] or ["- None"])
        lines.extend(["", "## Command Activity", ""])
        lines.extend(
            [
                f"- {item['created_at']} [{item['status']}] `{item['command_text']}`"
                + (f": {item['summary']}" if item.get("summary") else "")
                for item in command_events
            ]
            or ["- None"]
        )
        lines.extend(["", "## New Decisions", ""])
        lines.extend([f"- [{item['id']}] {item['title']}: {item['decision']}" for item in decisions] or ["- None"])
        lines.extend(["", "## Blocker Changes", ""])
        lines.extend([f"- [{item['id']}] {item['title']} ({item['status']}): {item['description']}" for item in blockers] or ["- None"])
        lines.extend(["", "## Handoffs Since Reference", ""])
        lines.extend([f"- [{item['id']}] {item['from_actor']} -> {item['to_actor']}: {item['summary']}" for item in handoffs] or ["- None"])
        lines.extend(["", "## Changed Files", ""])
        lines.extend([f"- {item}" for item in file_candidates] or ["- None"])
        lines.extend(["", "## Recommended Semantic Lookups", ""])
        lines.extend([f"- {item['entity_key']}: {item['summary_hint'] or item['name']}" for item in semantic] or ["- None"])
        markdown = "\n".join(lines).strip() + "\n"
        metadata = {
            "reference_kind": reference_kind,
            "reference_time": reference_time,
            "task_id": (task or {}).get("id"),
            "changed_task_ids": [item["id"] for item in tasks],
            "changed_files": file_candidates,
            "semantic_entity_keys": [item["entity_key"] for item in semantic],
            "counts": {
                "tasks": len(tasks),
                "work_logs": len(recent_work),
                "command_events": len(command_events),
                "decisions": len(decisions),
                "blockers": len(blockers),
                "handoffs": len(handoffs),
            },
        }
        fingerprints = {fp["file_path"]: fp["fingerprint"] for fp in store.get_file_fingerprints().values()}
        metadata["fingerprint_set"] = fingerprints
        artifact = store.upsert_context_artifact(
            artifact_key=f"delta_context:{scope_key}:{params_signature}",
            artifact_type="delta_context",
            scope_key=scope_key,
            params_signature=params_signature,
            state_version=state_version,
            content=markdown,
            metadata=metadata,
        )
        result = {
            "cached": False,
            "markdown": markdown,
            "state_version": state_version,
            "metadata": (artifact or {}).get("metadata", metadata),
        }
        self._record_token_usage_metric(
            operation="generate_delta_context",
            project_path=project_path,
            estimated_output_tokens=self._estimated_tokens(markdown),
            compact_output_tokens=self._estimated_tokens(markdown),
            compact_chars=len(markdown),
            metadata={
                "cached": False,
                "scope_key": scope_key,
                "reference_kind": metadata.get("reference_kind"),
                "state_version": state_version,
                "counts": metadata.get("counts", {}),
            },
        )
        return result

    def record_token_usage(
        self,
        operation: str,
        project_path: str | None = None,
        event_type: str = "provider_usage",
        actor: str = "obsmcp",
        session_id: str | None = None,
        task_id: str | None = None,
        model_name: str = "",
        provider: str = "",
        client_name: str = "",
        raw_input_tokens: int = 0,
        raw_output_tokens: int = 0,
        estimated_input_tokens: int = 0,
        estimated_output_tokens: int = 0,
        compact_input_tokens: int = 0,
        compact_output_tokens: int = 0,
        saved_tokens: int = 0,
        cache_creation_input_tokens: int = 0,
        cache_read_input_tokens: int = 0,
        raw_chars: int = 0,
        compact_chars: int = 0,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        return self._record_token_usage_metric(
            operation=operation,
            project_path=project_path,
            event_type=event_type,
            actor=actor,
            session_id=session_id,
            task_id=task_id,
            model_name=model_name,
            provider=provider,
            client_name=client_name,
            raw_input_tokens=raw_input_tokens,
            raw_output_tokens=raw_output_tokens,
            estimated_input_tokens=estimated_input_tokens,
            estimated_output_tokens=estimated_output_tokens,
            compact_input_tokens=compact_input_tokens,
            compact_output_tokens=compact_output_tokens,
            saved_tokens=saved_tokens,
            cache_creation_input_tokens=cache_creation_input_tokens,
            cache_read_input_tokens=cache_read_input_tokens,
            raw_chars=raw_chars,
            compact_chars=compact_chars,
            metadata=metadata,
        )

    def get_token_usage_stats(
        self,
        limit: int = 200,
        operation: str | None = None,
        session_id: str | None = None,
        project_path: str | None = None,
    ) -> dict[str, Any]:
        return self._store(project_path).get_token_usage_stats(limit=limit, operation=operation, session_id=session_id)

    def web_search(
        self,
        query: str,
        max_results: int | None = None,
        actor: str = "obsmcp",
        session_id: str | None = None,
        task_id: str | None = None,
        client_name: str = "",
        project_path: str | None = None,
    ) -> dict[str, Any]:
        result = get_opusmax_tool_provider().web_search(query=query, max_results=max_results)
        self._record_token_usage_metric(
            operation="web_search",
            project_path=project_path,
            event_type="provider_usage",
            actor=actor,
            session_id=session_id,
            task_id=task_id,
            provider="opusmax",
            client_name=client_name,
            raw_chars=len(query),
            compact_chars=len(result.get("summary", "")),
            metadata={
                "request_id": result.get("request_id"),
                "endpoint": result["endpoint"],
                "result_count": len(result.get("results") or []),
                "latency_ms": result["latency_ms"],
            },
        )
        return result

    def understand_image(
        self,
        prompt: str,
        image_url: str | None = None,
        image_path: str | None = None,
        image_base64: str | None = None,
        mime_type: str | None = None,
        actor: str = "obsmcp",
        session_id: str | None = None,
        task_id: str | None = None,
        client_name: str = "",
        project_path: str | None = None,
    ) -> dict[str, Any]:
        result = get_opusmax_tool_provider().understand_image(
            prompt=prompt,
            image_url=image_url,
            image_path=image_path,
            image_base64=image_base64,
            mime_type=mime_type,
        )
        analysis_text = result.get("analysis")
        compact_chars = len(analysis_text) if isinstance(analysis_text, str) else len(json.dumps(analysis_text, ensure_ascii=True))
        self._record_token_usage_metric(
            operation="understand_image",
            project_path=project_path,
            event_type="provider_usage",
            actor=actor,
            session_id=session_id,
            task_id=task_id,
            provider="opusmax",
            client_name=client_name,
            raw_chars=len(prompt),
            compact_chars=compact_chars,
            metadata={
                "request_id": result.get("request_id"),
                "endpoint": result["endpoint"],
                "latency_ms": result["latency_ms"],
                "image_source": result.get("image_source", {}),
            },
        )
        return result

    def get_raw_output_capture(
        self,
        capture_id: str,
        include_content: bool = False,
        project_path: str | None = None,
    ) -> dict[str, Any] | None:
        capture = self._store(project_path).get_raw_output_capture(capture_id)
        if not capture:
            return None
        if include_content:
            path = Path(capture["output_path"])
            capture["content"] = path.read_text(encoding="utf-8") if path.exists() else ""
        return capture

    def record_command_event(
        self,
        command_text: str,
        actor: str = "obsmcp",
        cwd: str = "",
        event_kind: str = "completed",
        status: str | None = None,
        risk_level: str = "normal",
        exit_code: int = 0,
        duration_ms: int = 0,
        output: str = "",
        stdout: str = "",
        stderr: str = "",
        summary: str | None = None,
        stdout_summary: str | None = None,
        stderr_summary: str | None = None,
        profile: str | None = None,
        policy_mode: str = "balanced",
        files_changed: list[str] | None = None,
        capture_raw_on_failure: bool = True,
        capture_raw_on_truncation: bool = True,
        session_id: str | None = None,
        task_id: str | None = None,
        metadata: dict[str, Any] | None = None,
        sync_mode: str = "deferred",
        project_path: str | None = None,
    ) -> dict[str, Any]:
        store = self._store(project_path)
        task = store.get_task(task_id) if task_id else store.get_current_task()
        effective_task_id = task["id"] if task else task_id
        effective_profile = profile or self._classify_tool_output_profile(command_text)
        effective_status = (status or ("failed" if exit_code else "completed")).lower()
        if risk_level == "normal":
            risk_level = self._classify_command_execution_risk(command_text)["risk_level"]
        stream_stdout = stdout or output or ""
        stream_stderr = stderr or ""
        policy = self._build_optimization_policy(
            mode=policy_mode,
            task=task,
            command_profile=effective_profile,
            exit_code=exit_code,
        )

        if stream_stdout and not stdout_summary:
            stdout_result = self._summarize_command_stream(
                output=stream_stdout,
                profile=effective_profile,
                exit_code=exit_code,
                policy=policy,
            )
        else:
            normalized_stdout_summary = (stdout_summary or "").strip()
            stdout_result = {
                "summary": normalized_stdout_summary + ("\n" if normalized_stdout_summary else ""),
                "raw_chars": len(stream_stdout),
                "compact_chars": len(normalized_stdout_summary),
                "raw_tokens_est": self._estimated_tokens(stream_stdout),
                "compact_tokens_est": self._estimated_tokens(normalized_stdout_summary),
                "raw_lines": len(self._strip_ansi(stream_stdout).splitlines()) if stream_stdout else 0,
                "compact_lines": len(normalized_stdout_summary.splitlines()),
                "was_compacted": bool(stream_stdout and normalized_stdout_summary and self._strip_ansi(stream_stdout).strip() != normalized_stdout_summary),
                "profile_info": {"provided_summary": bool(stdout_summary)},
            }

        if stream_stderr and not stderr_summary:
            stderr_result = self._summarize_command_stream(
                output=stream_stderr,
                profile="logs",
                exit_code=exit_code,
                policy=policy,
            )
        else:
            normalized_stderr_summary = (stderr_summary or "").strip()
            stderr_result = {
                "summary": normalized_stderr_summary + ("\n" if normalized_stderr_summary else ""),
                "raw_chars": len(stream_stderr),
                "compact_chars": len(normalized_stderr_summary),
                "raw_tokens_est": self._estimated_tokens(stream_stderr),
                "compact_tokens_est": self._estimated_tokens(normalized_stderr_summary),
                "raw_lines": len(self._strip_ansi(stream_stderr).splitlines()) if stream_stderr else 0,
                "compact_lines": len(normalized_stderr_summary.splitlines()),
                "was_compacted": bool(stream_stderr and normalized_stderr_summary and self._strip_ansi(stream_stderr).strip() != normalized_stderr_summary),
                "profile_info": {"provided_summary": bool(stderr_summary)},
            }

        combined_output = "\n".join(part for part in [stream_stdout, stream_stderr] if part).strip()
        combined_summary = "\n\n".join(
            part.strip()
            for part in [stdout_result["summary"], stderr_result["summary"]]
            if part and part.strip()
        ).strip()
        raw_capture: dict[str, Any] | None = None
        capture_reason = ""
        if combined_output:
            raw_text = self._strip_ansi(combined_output).strip()
            compact_text = combined_summary.strip()
            if exit_code != 0 and capture_raw_on_failure and policy.get("raw_capture_on_failure", True):
                capture_reason = "failure"
            elif compact_text and raw_text and compact_text != raw_text and capture_raw_on_truncation and policy.get("raw_capture_on_truncation", True):
                capture_reason = "truncation"
            if capture_reason:
                raw_capture = self._store_raw_output_capture(
                    command=command_text,
                    output=combined_output,
                    profile=effective_profile,
                    reason=capture_reason,
                    project_path=project_path,
                    actor=actor,
                    session_id=session_id,
                    task_id=effective_task_id,
                    exit_code=exit_code,
                    metadata={
                        "policy_mode": policy_mode,
                        "stdout_profile_info": stdout_result["profile_info"],
                        "stderr_profile_info": stderr_result["profile_info"],
                    },
                )

        effective_summary = summary or self._build_command_event_summary(
            command_text=command_text,
            status=effective_status,
            exit_code=exit_code,
            duration_ms=duration_ms,
            stdout_summary=stdout_result["summary"],
            stderr_summary=stderr_result["summary"],
            raw_capture=raw_capture,
        )
        total_raw_tokens = stdout_result["raw_tokens_est"] + stderr_result["raw_tokens_est"]
        total_compact_tokens = stdout_result["compact_tokens_est"] + stderr_result["compact_tokens_est"]
        event = store.record_command_event(
            actor=actor,
            command_text=command_text,
            cwd=cwd,
            event_kind=event_kind,
            status=effective_status,
            risk_level=risk_level,
            exit_code=exit_code,
            duration_ms=duration_ms,
            summary=effective_summary,
            stdout_summary=stdout_result["summary"],
            stderr_summary=stderr_result["summary"],
            output_profile=effective_profile,
            raw_capture_id=(raw_capture or {}).get("capture_id"),
            raw_output_available=raw_capture is not None,
            files_changed=files_changed,
            metadata={
                **(metadata or {}),
                "policy_mode": policy_mode,
                "policy": policy,
                "capture_reason": capture_reason or None,
                "stdout_profile_info": stdout_result["profile_info"],
                "stderr_profile_info": stderr_result["profile_info"],
            },
            session_id=session_id,
            task_id=effective_task_id,
        ) or {}
        self._record_token_usage_metric(
            operation="record_command_event",
            project_path=project_path,
            event_type="command_history",
            actor=actor,
            session_id=session_id,
            task_id=effective_task_id,
            estimated_output_tokens=total_raw_tokens,
            compact_output_tokens=total_compact_tokens,
            saved_tokens=max(total_raw_tokens - total_compact_tokens, 0),
            raw_chars=stdout_result["raw_chars"] + stderr_result["raw_chars"],
            compact_chars=stdout_result["compact_chars"] + stderr_result["compact_chars"],
            metadata={
                "command": command_text,
                "status": effective_status,
                "exit_code": exit_code,
                "profile": effective_profile,
                "capture_reason": capture_reason or None,
                "event_id": event.get("id"),
            },
        )
        sync_result = self._sync_after_write(project_path, sync_mode=sync_mode)
        event["raw_capture"] = raw_capture
        event["sync"] = sync_result
        return event

    def record_command_batch(
        self,
        commands: list[dict[str, Any]],
        actor: str = "obsmcp",
        session_id: str | None = None,
        task_id: str | None = None,
        policy_mode: str = "balanced",
        batch_label: str = "",
        sync_mode: str = "deferred",
        project_path: str | None = None,
    ) -> dict[str, Any]:
        batch_id = f"BATCH-{uuid.uuid4().hex[:10].upper()}"
        recorded: list[dict[str, Any]] = []
        summary_lines: list[str] = []
        risk_counts = {"low": 0, "medium": 0, "high": 0}
        for idx, item in enumerate(commands or []):
            command_text = str(item.get("command_text") or item.get("command") or "").strip()
            if not command_text:
                continue
            policy = self.get_command_execution_policy(
                command=command_text,
                task_id=task_id,
                mode=policy_mode,
                exit_code=int(item.get("exit_code") or 0),
                project_path=project_path,
            )
            risk_counts[policy["risk_level"]] = risk_counts.get(policy["risk_level"], 0) + 1
            event = self.record_command_event(
                command_text=command_text,
                actor=item.get("actor", actor),
                cwd=item.get("cwd", ""),
                event_kind=item.get("event_kind", "batch_member"),
                status=item.get("status"),
                risk_level=item.get("risk_level", policy["risk_level"]),
                exit_code=int(item.get("exit_code") or 0),
                duration_ms=int(item.get("duration_ms") or 0),
                output=item.get("output", ""),
                stdout=item.get("stdout", ""),
                stderr=item.get("stderr", ""),
                summary=item.get("summary"),
                stdout_summary=item.get("stdout_summary"),
                stderr_summary=item.get("stderr_summary"),
                profile=item.get("profile"),
                policy_mode=item.get("policy_mode", policy_mode),
                files_changed=item.get("files_changed"),
                capture_raw_on_failure=bool(item.get("capture_raw_on_failure", True)),
                capture_raw_on_truncation=bool(item.get("capture_raw_on_truncation", True)),
                session_id=item.get("session_id", session_id),
                task_id=item.get("task_id", task_id),
                metadata={
                    "batch_id": batch_id,
                    "batch_index": idx,
                    "batch_label": batch_label,
                },
                sync_mode="none",
                project_path=project_path,
            )
            recorded.append(event)
            summary_lines.append(f"- [{policy['risk_level']}/{event['status']}] `{command_text}`")
        sync_result = self._sync_after_write(project_path, sync_mode=sync_mode)
        return {
            "batch_id": batch_id,
            "batch_label": batch_label,
            "command_count": len(recorded),
            "commands": recorded,
            "risk_counts": risk_counts,
            "summary": "\n".join(summary_lines) if summary_lines else "- None",
            "sync": sync_result,
        }

    def get_command_event(self, event_id: int, project_path: str | None = None) -> dict[str, Any] | None:
        return self._store(project_path).get_command_event(event_id)

    def get_recent_commands(
        self,
        limit: int = 20,
        after_id: int | None = None,
        session_id: str | None = None,
        task_id: str | None = None,
        status: str | None = None,
        actor: str | None = None,
        project_path: str | None = None,
    ) -> dict[str, Any]:
        effective_task_id = task_id
        if not effective_task_id and session_id:
            session = self._store(project_path).get_session(session_id)
            if session and session.get("task_id"):
                effective_task_id = session["task_id"]
        events = self._store(project_path).list_command_events(
            limit=limit,
            after_id=after_id,
            session_id=session_id,
            task_id=effective_task_id,
            status=status,
            actor=actor,
        )
        return {
            "events": events,
            "has_more": len(events) == limit,
            "next_cursor": events[-1]["id"] if events else None,
        }

    def get_last_command_result(
        self,
        session_id: str | None = None,
        task_id: str | None = None,
        actor: str | None = None,
        project_path: str | None = None,
    ) -> dict[str, Any] | None:
        effective_task_id = task_id
        if not effective_task_id and session_id:
            session = self._store(project_path).get_session(session_id)
            if session and session.get("task_id"):
                effective_task_id = session["task_id"]
        return self._store(project_path).get_last_command_result(
            session_id=session_id,
            task_id=effective_task_id,
            actor=actor,
        )

    def get_command_failures(
        self,
        limit: int = 20,
        session_id: str | None = None,
        task_id: str | None = None,
        actor: str | None = None,
        project_path: str | None = None,
    ) -> list[dict[str, Any]]:
        effective_task_id = task_id
        if not effective_task_id and session_id:
            session = self._store(project_path).get_session(session_id)
            if session and session.get("task_id"):
                effective_task_id = session["task_id"]
        return self._store(project_path).get_command_failures(
            limit=limit,
            session_id=session_id,
            task_id=effective_task_id,
            actor=actor,
        )

    def compact_tool_output(
        self,
        command: str,
        output: str,
        exit_code: int = 0,
        profile: str | None = None,
        policy_mode: str = "balanced",
        actor: str = "obsmcp",
        session_id: str | None = None,
        task_id: str | None = None,
        capture_raw_on_failure: bool = True,
        capture_raw_on_truncation: bool = True,
        project_path: str | None = None,
    ) -> dict[str, Any]:
        store = self._store(project_path)
        task = store.get_task(task_id) if task_id else store.get_current_task()
        effective_profile = profile or self._classify_tool_output_profile(command)
        policy = self._build_optimization_policy(
            mode=policy_mode,
            task=task,
            command_profile=effective_profile,
            exit_code=exit_code,
        )
        compact_output, profile_info = self._compact_tool_output_lines(
            profile=effective_profile,
            output=output,
            exit_code=exit_code,
            policy=policy,
        )
        raw_output_stripped = self._strip_ansi(output).strip() + ("\n" if output.strip() else "")
        raw_chars = len(output)
        compact_chars = len(compact_output)
        raw_tokens_est = self._estimated_tokens(output)
        compact_tokens_est = self._estimated_tokens(compact_output)
        line_count = len(raw_output_stripped.splitlines())
        compact_line_count = len(compact_output.splitlines())
        was_compacted = compact_output.strip() != raw_output_stripped.strip()
        raw_capture: dict[str, Any] | None = None
        capture_reason = ""
        if exit_code != 0 and capture_raw_on_failure and policy.get("raw_capture_on_failure", True):
            capture_reason = "failure"
        elif was_compacted and capture_raw_on_truncation and policy.get("raw_capture_on_truncation", True):
            capture_reason = "truncation"
        if capture_reason:
            raw_capture = self._store_raw_output_capture(
                command=command,
                output=output,
                profile=effective_profile,
                reason=capture_reason,
                project_path=project_path,
                actor=actor,
                session_id=session_id,
                task_id=task_id,
                exit_code=exit_code,
                metadata={
                    "raw_lines": line_count,
                    "compact_lines": compact_line_count,
                    "profile_info": profile_info,
                },
            )
            if raw_capture:
                compact_output = compact_output.rstrip() + (
                    "\n\n[raw output saved]\n"
                    f"- capture_id: {raw_capture['capture_id']}\n"
                    f"- path: {raw_capture['output_path']}\n"
                )
        saved_tokens = max(raw_tokens_est - compact_tokens_est, 0)
        metrics = self._record_token_usage_metric(
            operation="compact_tool_output",
            project_path=project_path,
            event_type="tool_compaction",
            actor=actor,
            session_id=session_id,
            task_id=task_id,
            estimated_output_tokens=raw_tokens_est,
            compact_output_tokens=compact_tokens_est,
            saved_tokens=saved_tokens,
            raw_chars=raw_chars,
            compact_chars=len(compact_output),
            metadata={
                "command": command,
                "profile": effective_profile,
                "policy_mode": policy_mode,
                "exit_code": exit_code,
                "was_compacted": was_compacted,
                "capture_reason": capture_reason or None,
                "policy": policy,
                "profile_info": profile_info,
            },
        )
        return {
            "command": command,
            "profile": effective_profile,
            "policy_mode": policy_mode,
            "policy": policy,
            "exit_code": exit_code,
            "output": compact_output,
            "compact_output": compact_output,
            "raw_chars": raw_chars,
            "compact_chars": len(compact_output),
            "raw_tokens_est": raw_tokens_est,
            "compact_tokens_est": compact_tokens_est,
            "saved_tokens_est": saved_tokens,
            "raw_lines": line_count,
            "compact_lines": compact_line_count,
            "was_compacted": was_compacted,
            "raw_capture": raw_capture,
            "profile_info": profile_info,
            "metric": metrics,
        }

    def compact_response(
        self,
        text: str,
        level: str = "full",
        preserve_code: bool = True,
        actor: str = "obsmcp",
        project_path: str | None = None,
    ) -> dict[str, Any]:
        """
        Compress text output using rule-based patterns.

        Args:
            text: The text to compress
            level: Compression level - "lite", "full", or "ultra"
            preserve_code: If True, code blocks are preserved exactly
            actor: Actor name for tracking
            project_path: Project path for tracking

        Returns:
            Dictionary with compressed text and compression stats
        """
        if not text or not text.strip():
            return {
                "original": text,
                "compressed": text,
                "original_length": 0,
                "compressed_length": 0,
                "saved_ratio": 0.0,
                "was_compressed": False,
            }

        # Apply compression
        if preserve_code:
            result = compress_preserve_code(text, level)
        else:
            result = compress(text, level)

        # Record metrics if project path provided
        if project_path:
            saved_chars = result.original_length - result.compressed_length
            self._record_token_usage_metric(
                operation="compact_response",
                project_path=project_path,
                event_type="output_compression",
                actor=actor,
                raw_chars=result.original_length,
                compact_chars=result.compressed_length,
                metadata={
                    "level": level,
                    "preserve_code": preserve_code,
                    "saved_ratio": result.saved_ratio,
                },
            )

        return {
            "original": text,
            "compressed": result.compressed,
            "original_length": result.original_length,
            "compressed_length": result.compressed_length,
            "saved_ratio": round(result.saved_ratio, 4),
            "was_compressed": result.was_compressed,
            "compression_level": level,
        }

    def generate_prompt_segments(
        self,
        profile: str = "balanced",
        task_id: str | None = None,
        max_tokens: int | None = 2600,
        project_path: str | None = None,
    ) -> dict[str, Any]:
        store = self._store(project_path)
        task = store.get_task(task_id) if task_id else store.get_current_task()
        params = {
            "profile": profile.lower(),
            "task_id": task["id"] if task else task_id,
            "max_tokens": max_tokens,
        }
        params_signature = self._artifact_signature(params)
        scope_key = self._context_scope_key(task)
        state_version = store.get_context_state_version(include_semantic=True)
        cached = store.get_context_artifact("prompt_segments", scope_key, params_signature)
        if cached and cached["state_version"] == state_version and not store.is_context_stale(cached):
            metadata = dict(cached.get("metadata", {}))
            metadata["cached"] = True
            combined_markdown = cached["content"]
            self._record_token_usage_metric(
                operation="generate_prompt_segments",
                project_path=project_path,
                estimated_output_tokens=int(metadata.get("combined_tokens") or self._estimated_tokens(combined_markdown)),
                compact_output_tokens=int(metadata.get("combined_tokens") or self._estimated_tokens(combined_markdown)),
                compact_chars=len(combined_markdown),
                metadata={
                    "cached": True,
                    "scope_key": scope_key,
                    "profile": profile.lower(),
                },
            )
            return {
                "profile": profile.lower(),
                "scope_key": scope_key,
                "stable_markdown": metadata.get("stable_markdown", ""),
                "dynamic_markdown": metadata.get("dynamic_markdown", ""),
                "combined_markdown": combined_markdown,
                "cached": True,
                "state_version": state_version,
                "metadata": metadata,
            }

        data = self._collect_context_data(
            task_id=task["id"] if task else task_id,
            project_path=project_path,
            recent_work_limit=8,
            decisions_limit=6,
            blockers_limit=6,
            relevant_files_limit=12,
            semantic_limit=6,
            active_sessions_limit=4,
            active_tasks_limit=8,
            include_dependency_map=False,
            include_daily_notes=False,
            include_session_info=False,
            include_semantic=True,
        )
        brief = data["brief"]
        symbol_stats = store.get_symbol_index_stats()
        stable_sections = [
            {"name": "stable_header", "layer": "stable", "priority": 1, "text": "# Stable Prompt Prefix"},
            {"name": "mission", "layer": "stable", "priority": 2, "text": f"## Mission\n{brief.get('Mission', 'No mission recorded yet.').strip() or 'No mission recorded yet.'}"},
            {"name": "success_criteria", "layer": "stable", "priority": 3, "text": f"## Success Criteria\n{brief.get('Success Criteria', '').strip() or 'No success criteria recorded yet.'}"},
            {"name": "architecture", "layer": "stable", "priority": 4, "text": f"## Architecture\n{brief.get('Architecture', '').strip() or 'No architecture summary recorded yet.'}"},
            {"name": "working_agreements", "layer": "stable", "priority": 5, "text": f"## Working Agreements\n{brief.get('Working Agreements', '').strip() or 'No working agreements recorded yet.'}"},
        ]
        if symbol_stats.get("entity_count"):
            stable_sections.append(
                {
                    "name": "atlas_snapshot",
                    "layer": "stable",
                    "priority": 6,
                    "text": "\n".join(
                        [
                            "## Code Atlas Snapshot",
                            f"- Entities: {symbol_stats.get('entity_count', 0)}",
                            f"- Files: {symbol_stats.get('file_count', 0)}",
                            f"- Features: {symbol_stats.get('feature_count', 0)}",
                        ]
                    ),
                }
            )
        stable_sections = self._apply_section_order_policy(stable_sections, task=task, mode=profile.lower())

        current_task_lines = ["## Current Task"]
        if task:
            current_task_lines.extend(
                [
                    f"- ID: {task['id']}",
                    f"- Title: {task['title']}",
                    f"- Status: {task['status']}",
                    f"- Priority: {task['priority']}",
                    "",
                    task["description"] or "No description.",
                ]
            )
        else:
            current_task_lines.append("- No current task is set.")
        dynamic_sections = [
            {"name": "dynamic_header", "layer": "dynamic", "priority": 1, "text": "# Dynamic Prompt Suffix"},
            {"name": "current_task", "layer": "dynamic", "priority": 2, "text": "\n".join(current_task_lines)},
            {"name": "relevant_files", "layer": "dynamic", "priority": 3, "text": "\n".join(["## Relevant Files"] + ([f"- {item}" for item in data["relevant_files"]] or ["- None"]))},
        ]
        handoff = data["latest_handoff"]
        handoff_lines = ["## Latest Handoff"]
        if handoff:
            handoff_lines.extend(
                [
                    f"- From: {handoff['from_actor']}",
                    f"- To: {handoff['to_actor']}",
                    f"- Created: {handoff['created_at']}",
                    "",
                    handoff["summary"] or "No summary.",
                    "",
                    f"Next Steps: {handoff['next_steps'] or 'None recorded.'}",
                ]
            )
        else:
            handoff_lines.append("- No handoff recorded yet.")
        dynamic_sections.extend(
            [
                {"name": "latest_handoff", "layer": "dynamic", "priority": 4, "text": "\n".join(handoff_lines)},
                {"name": "blockers", "layer": "dynamic", "priority": 5, "text": "\n".join(["## Open Blockers"] + ([f"- [{item['id']}] {item['title']}: {item['description']}" for item in data["blockers"]] or ["- None"]))},
                {"name": "recent_work", "layer": "dynamic", "priority": 6, "text": "\n".join(["## Recent Work"] + ([f"- {item['created_at']} [{item['actor']}] {item['message']}" for item in data["recent_work"]] or ["- None"]))},
                {"name": "recent_decisions", "layer": "dynamic", "priority": 7, "text": "\n".join(["## Recent Decisions"] + ([f"- [{item['id']}] {item['title']}: {item['decision']}" for item in data["decisions"]] or ["- None"]))},
                {"name": "semantic", "layer": "dynamic", "priority": 8, "text": "\n".join(["## Recommended Semantic Lookups"] + ([f"- {item['entity_key']}: {item['summary_hint'] or item['name']}" for item in data["semantic_suggestions"]] or ["- None"]))},
            ]
        )
        dynamic_sections = self._apply_section_order_policy(dynamic_sections, task=task, mode=profile.lower())

        stable_budget = None
        if max_tokens and max_tokens > 0:
            stable_budget = min(max(max_tokens // 2, 200), max_tokens)
        stable_markdown, stable_used = self._render_context_sections(stable_sections, max_tokens=stable_budget)
        if max_tokens and max_tokens > 0:
            remaining_budget = max(max_tokens - stable_used, 0)
            if remaining_budget > 0:
                dynamic_markdown, dynamic_used = self._render_context_sections(dynamic_sections, max_tokens=remaining_budget)
            else:
                dynamic_markdown, dynamic_used = "", 0
        else:
            dynamic_markdown, dynamic_used = self._render_context_sections(dynamic_sections, max_tokens=None)
        combined_markdown = f"{stable_markdown.rstrip()}\n\n{dynamic_markdown.lstrip()}".strip() + "\n"
        metadata = {
            "profile": profile.lower(),
            "task_id": task["id"] if task else None,
            "stable_tokens": stable_used,
            "dynamic_tokens": dynamic_used,
            "combined_tokens": stable_used + dynamic_used,
            "stable_sections": [section["name"] for section in stable_sections],
            "dynamic_sections": [section["name"] for section in dynamic_sections],
            "stable_markdown": stable_markdown,
            "dynamic_markdown": dynamic_markdown,
            "max_tokens": max_tokens,
        }
        artifact = store.upsert_context_artifact(
            artifact_key=f"prompt_segments:{scope_key}:{params_signature}",
            artifact_type="prompt_segments",
            scope_key=scope_key,
            params_signature=params_signature,
            state_version=state_version,
            content=combined_markdown,
            metadata=metadata,
        )
        self._record_token_usage_metric(
            operation="generate_prompt_segments",
            project_path=project_path,
            estimated_output_tokens=stable_used + dynamic_used,
            compact_output_tokens=stable_used + dynamic_used,
            compact_chars=len(combined_markdown),
            metadata={
                "cached": False,
                "scope_key": scope_key,
                "profile": profile.lower(),
                "stable_sections": metadata["stable_sections"],
                "dynamic_sections": metadata["dynamic_sections"],
            },
        )
        final_metadata = (artifact or {}).get("metadata", metadata)
        return {
            "profile": profile.lower(),
            "scope_key": scope_key,
            "stable_markdown": final_metadata.get("stable_markdown", stable_markdown),
            "dynamic_markdown": final_metadata.get("dynamic_markdown", dynamic_markdown),
            "combined_markdown": combined_markdown,
            "cached": False,
            "state_version": state_version,
            "metadata": final_metadata,
        }

    def generate_retrieval_context(
        self,
        query: str,
        task_id: str | None = None,
        max_tokens: int | None = 1800,
        include_delta: bool = True,
        include_semantic: bool = True,
        project_path: str | None = None,
    ) -> dict[str, Any]:
        store = self._store(project_path)
        task = store.get_task(task_id) if task_id else store.get_current_task()
        effective_query = query.strip() or ((task or {}).get("title", "").strip()) or "current task"
        params = {
            "query": effective_query,
            "task_id": task["id"] if task else task_id,
            "max_tokens": max_tokens,
            "include_delta": include_delta,
            "include_semantic": include_semantic,
        }
        params_signature = self._artifact_signature(params)
        scope_key = self._context_scope_key(task)
        state_version = store.get_context_state_version(include_semantic=True)
        cached = store.get_context_artifact("retrieval_context", scope_key, params_signature)
        if cached and cached["state_version"] == state_version and not store.is_context_stale(cached):
            metadata = dict(cached.get("metadata", {}))
            metadata["cached"] = True
            self._record_token_usage_metric(
                operation="generate_retrieval_context",
                project_path=project_path,
                estimated_output_tokens=int(metadata.get("used_tokens") or self._estimated_tokens(cached["content"])),
                compact_output_tokens=int(metadata.get("used_tokens") or self._estimated_tokens(cached["content"])),
                compact_chars=len(cached["content"]),
                metadata={
                    "cached": True,
                    "query": effective_query,
                    "scope_key": scope_key,
                },
            )
            return {
                "query": effective_query,
                "scope_key": scope_key,
                "markdown": cached["content"],
                "cached": True,
                "state_version": state_version,
                "metadata": metadata,
            }

        terms = self._query_terms(effective_query)
        data = self._collect_context_data(
            task_id=task["id"] if task else task_id,
            project_path=project_path,
            recent_work_limit=12,
            decisions_limit=10,
            blockers_limit=8,
            relevant_files_limit=16,
            semantic_limit=8,
            active_sessions_limit=3,
            active_tasks_limit=10,
            include_dependency_map=False,
            include_daily_notes=False,
            include_session_info=False,
            include_semantic=include_semantic,
        )
        ranked_work = self._rank_values(
            data["recent_work"],
            terms=terms,
            text_getter=lambda item: f"{item.get('message', '')} {' '.join(item.get('files', []))}",
            limit=6,
        )
        ranked_decisions = self._rank_values(
            data["decisions"],
            terms=terms,
            text_getter=lambda item: f"{item.get('title', '')} {item.get('decision', '')}",
            limit=6,
        )
        ranked_blockers = self._rank_values(
            data["blockers"],
            terms=terms,
            text_getter=lambda item: f"{item.get('title', '')} {item.get('description', '')}",
            limit=5,
        )
        ranked_files = self._rank_values(
            data["relevant_files"],
            terms=terms,
            text_getter=lambda item: str(item),
            limit=8,
        )
        semantic_results = data["semantic_suggestions"]
        if include_semantic and effective_query:
            try:
                semantic_results = self.search_code_knowledge(effective_query, limit=8, project_path=project_path).get("results", [])
            except Exception:
                semantic_results = data["semantic_suggestions"]
        delta = self.generate_delta_context(task_id=(task or {}).get("id"), project_path=project_path) if include_delta else None
        delta_meta = (delta or {}).get("metadata", {})

        sections = [
            {
                "name": "header",
                "layer": "R0",
                "priority": 1,
                "text": "\n".join(
                    [
                        "# Retrieval Context",
                        f"Query: {effective_query}",
                        f"Generated: {utc_now()}",
                    ]
                ),
            },
            {
                "name": "current_task",
                "layer": "R0",
                "priority": 2,
                "text": "\n".join(
                    [
                        "## Current Task",
                        f"- ID: {(task or {}).get('id', 'none')}",
                        f"- Title: {(task or {}).get('title', 'No current task')}",
                        f"- Status: {(task or {}).get('status', 'unknown')}",
                        "",
                        (task or {}).get("description", "No task description."),
                    ]
                ),
            },
        ]
        if include_delta and delta:
            sections.append(
                {
                    "name": "delta",
                    "layer": "R0",
                    "priority": 3,
                    "text": "\n".join(
                        [
                            "## Delta-First Summary",
                            f"- Reference Kind: {delta_meta.get('reference_kind', 'unknown')}",
                            f"- Changed Tasks: {delta_meta.get('counts', {}).get('tasks', 0)}",
                            f"- New Work Logs: {delta_meta.get('counts', {}).get('work_logs', 0)}",
                            f"- Decision Changes: {delta_meta.get('counts', {}).get('decisions', 0)}",
                            f"- Matching Files: {', '.join(ranked_files[:5]) or 'None'}",
                        ]
                    ),
                }
            )
        sections.extend(
            [
                {
                    "name": "relevant_files",
                    "layer": "R1",
                    "priority": 4,
                    "text": "\n".join(["## Ranked Relevant Files"] + ([f"- {item}" for item in ranked_files] or ["- None"])),
                },
                {
                    "name": "recent_work",
                    "layer": "R1",
                    "priority": 5,
                    "text": "\n".join(["## Ranked Recent Work"] + ([f"- {item['created_at']} [{item['actor']}] {item['message']}" for item in ranked_work] or ["- None"])),
                },
                {
                    "name": "decisions",
                    "layer": "R1",
                    "priority": 6,
                    "text": "\n".join(["## Ranked Decisions"] + ([f"- [{item['id']}] {item['title']}: {item['decision']}" for item in ranked_decisions] or ["- None"])),
                },
                {
                    "name": "blockers",
                    "layer": "R1",
                    "priority": 7,
                    "text": "\n".join(["## Ranked Blockers"] + ([f"- [{item['id']}] {item['title']}: {item['description']}" for item in ranked_blockers] or ["- None"])),
                },
                {
                    "name": "semantic",
                    "layer": "R2",
                    "priority": 8,
                    "text": "\n".join(
                        ["## Ranked Semantic Results"]
                        + (
                            [
                                f"- {item.get('entity_key', item.get('name', 'unknown'))}: {item.get('summary_hint') or item.get('name', 'No summary')}"
                                for item in semantic_results[:8]
                            ]
                            or ["- None"]
                        )
                    ),
                },
            ]
        )
        sections = self._apply_section_order_policy(sections, task=task, mode="balanced")
        markdown, used_tokens = self._render_context_sections(sections, max_tokens=max_tokens)
        metadata = {
            "query": effective_query,
            "task_id": (task or {}).get("id"),
            "used_tokens": used_tokens,
            "matched_files": ranked_files,
            "matched_work_ids": [item["id"] for item in ranked_work if item.get("id") is not None],
            "matched_decision_ids": [item["id"] for item in ranked_decisions if item.get("id") is not None],
            "matched_blocker_ids": [item["id"] for item in ranked_blockers if item.get("id") is not None],
            "semantic_entity_keys": [item.get("entity_key") for item in semantic_results[:8] if item.get("entity_key")],
            "section_order": [section["name"] for section in sections],
            "include_delta": include_delta,
            "include_semantic": include_semantic,
        }
        artifact = store.upsert_context_artifact(
            artifact_key=f"retrieval_context:{scope_key}:{params_signature}",
            artifact_type="retrieval_context",
            scope_key=scope_key,
            params_signature=params_signature,
            state_version=state_version,
            content=markdown,
            metadata=metadata,
        )
        self._record_token_usage_metric(
            operation="generate_retrieval_context",
            project_path=project_path,
            estimated_output_tokens=used_tokens,
            compact_output_tokens=used_tokens,
            compact_chars=len(markdown),
            metadata={
                "cached": False,
                "query": effective_query,
                "scope_key": scope_key,
                "section_order": metadata["section_order"],
            },
        )
        return {
            "query": effective_query,
            "scope_key": scope_key,
            "markdown": markdown,
            "cached": False,
            "state_version": state_version,
            "metadata": (artifact or {}).get("metadata", metadata),
        }

    def get_fast_path_response(
        self,
        kind: str,
        task_id: str | None = None,
        session_id: str | None = None,
        module_path: str | None = None,
        symbol_name: str | None = None,
        feature_name: str | None = None,
        query: str | None = None,
        as_markdown: bool = False,
        project_path: str | None = None,
    ) -> dict[str, Any]:
        normalized_kind = kind.lower()
        payload: Any
        markdown = ""
        if normalized_kind == "current_task":
            payload = self.get_current_task(project_path=project_path) or {}
            markdown = "\n".join(
                [
                    "# Fast Path: Current Task",
                    f"- ID: {payload.get('id', 'none')}",
                    f"- Title: {payload.get('title', 'No current task')}",
                    f"- Status: {payload.get('status', 'unknown')}",
                    f"- Priority: {payload.get('priority', 'unknown')}",
                    "",
                    payload.get("description", "No description."),
                ]
            ).strip() + "\n"
        elif normalized_kind == "blockers":
            payload = self.get_blockers(project_path=project_path)
            markdown = "\n".join(["# Fast Path: Blockers"] + ([f"- [{item['id']}] {item['title']}: {item['description']}" for item in payload] or ["- None"])) + "\n"
        elif normalized_kind == "relevant_files":
            payload = self.get_relevant_files(task_id=task_id, project_path=project_path)
            markdown = "\n".join(["# Fast Path: Relevant Files"] + ([f"- {item}" for item in payload] or ["- None"])) + "\n"
        elif normalized_kind == "task_snapshot":
            payload = self.generate_task_snapshot(task_id=task_id, project_path=project_path)
            markdown = json.dumps(payload, indent=2, ensure_ascii=True)
        elif normalized_kind == "project_status":
            payload = self.get_project_status_snapshot(project_path=project_path)
            markdown = json.dumps(payload, indent=2, ensure_ascii=True)
        elif normalized_kind == "resume_packet":
            payload = self.generate_resume_packet(session_id=session_id, task_id=task_id, project_path=project_path, write_files=False)
            markdown = payload["markdown"]
        elif normalized_kind == "startup_context":
            payload = self.generate_startup_context(session_id=session_id, task_id=task_id, project_path=project_path)
            markdown = payload["markdown"]
        elif normalized_kind == "startup_preflight":
            payload = self.get_startup_preflight(task_id=task_id, session_id=session_id, project_path=project_path)
            markdown = json.dumps(payload, indent=2, ensure_ascii=True)
        elif normalized_kind == "resume_board":
            payload = self.get_resume_board(project_path=project_path)
            markdown = json.dumps(payload, indent=2, ensure_ascii=True)
        elif normalized_kind == "recent_commands":
            payload = self.get_recent_commands(task_id=task_id, session_id=session_id, project_path=project_path)["events"]
            markdown = "\n".join(
                ["# Fast Path: Recent Commands"]
                + (
                    [
                        f"- {item.get('created_at', '')} [{item['status']}] `{item['command_text']}`"
                        + (f": {item['summary']}" if item.get("summary") else "")
                        for item in payload
                    ]
                    or ["- None"]
                )
            ) + "\n"
        elif normalized_kind == "last_command":
            payload = self.get_last_command_result(task_id=task_id, session_id=session_id, project_path=project_path) or {}
            markdown = "\n".join(
                [
                    "# Fast Path: Last Command",
                    f"- ID: {payload.get('id', 'none')}",
                    f"- Status: {payload.get('status', 'none')}",
                    f"- Exit Code: {payload.get('exit_code', 'none')}",
                    f"- Command: {payload.get('command_text', 'none')}",
                    "",
                    payload.get("summary", "No command result recorded."),
                ]
            ).strip() + "\n"
        elif normalized_kind == "command_failures":
            payload = self.get_command_failures(task_id=task_id, session_id=session_id, project_path=project_path)
            markdown = "\n".join(
                ["# Fast Path: Command Failures"]
                + (
                    [
                        f"- {item.get('created_at', '')} [exit {item['exit_code']}] `{item['command_text']}`"
                        + (f": {item['summary']}" if item.get("summary") else "")
                        for item in payload
                    ]
                    or ["- None"]
                )
            ) + "\n"
        elif normalized_kind == "retrieval":
            payload = self.generate_retrieval_context(query=query or "", task_id=task_id, project_path=project_path)
            markdown = payload["markdown"]
        elif normalized_kind == "semantic_lookup":
            if symbol_name:
                payload = self.describe_symbol(symbol_name=symbol_name, module_path=module_path, project_path=project_path)
            elif feature_name:
                payload = self.describe_feature(feature_name=feature_name, project_path=project_path)
            elif module_path:
                payload = self.describe_module(module_path=module_path, project_path=project_path)
            else:
                raise ValueError("semantic_lookup fast path requires module_path, symbol_name, or feature_name.")
            markdown = json.dumps(payload, indent=2, ensure_ascii=True)
        else:
            raise ValueError(f"Unknown fast path kind: {kind}")
        rendered = markdown if as_markdown else payload
        output_text = markdown if markdown else json.dumps(payload, ensure_ascii=True)
        self._record_token_usage_metric(
            operation="get_fast_path_response",
            project_path=project_path,
            estimated_output_tokens=self._estimated_tokens(output_text),
            compact_output_tokens=self._estimated_tokens(output_text),
            compact_chars=len(output_text),
            metadata={
                "kind": normalized_kind,
                "task_id": task_id,
                "session_id": session_id,
                "as_markdown": as_markdown,
            },
        )
        return {
            "kind": normalized_kind,
            "source": "deterministic",
            "task_id": task_id,
            "session_id": session_id,
            "as_markdown": as_markdown,
            "result": rendered,
            "markdown": markdown,
            "json": payload,
        }

    def _artifact_sections_for_chunking(
        self,
        *,
        artifact_type: str,
        profile: str,
        task_id: str | None,
        project_path: str | None,
        query: str | None = None,
    ) -> list[dict[str, Any]]:
        task = self._store(project_path).get_task(task_id) if task_id else self._store(project_path).get_current_task()
        effective_task_id = task["id"] if task else task_id
        if artifact_type == "context_profile":
            sections, _ = self._build_tiered_sections(profile, task_id=effective_task_id, project_path=project_path)
            return sections
        if artifact_type == "prompt_segments":
            segments = self.generate_prompt_segments(profile=profile, task_id=effective_task_id, project_path=project_path)
            return [
                {"name": "stable", "layer": "stable", "priority": 1, "text": segments["stable_markdown"].strip()},
                {"name": "dynamic", "layer": "dynamic", "priority": 2, "text": segments["dynamic_markdown"].strip()},
            ]
        if artifact_type == "delta_context":
            delta = self.generate_delta_context(task_id=effective_task_id, project_path=project_path)
            return self._split_markdown_sections(delta["markdown"], fallback_name="delta_context")
        if artifact_type == "retrieval_context":
            retrieval = self.generate_retrieval_context(query=query or "", task_id=effective_task_id, project_path=project_path)
            return self._split_markdown_sections(retrieval["markdown"], fallback_name="retrieval_context")
        if artifact_type == "resume_packet":
            resume = self.generate_resume_packet(task_id=effective_task_id, project_path=project_path, write_files=False)
            return self._split_markdown_sections(resume["markdown"], fallback_name="resume_packet")
        raise ValueError(f"Unsupported artifact_type for chunking: {artifact_type}")

    def list_context_chunks(
        self,
        artifact_type: str = "context_profile",
        profile: str = "deep",
        task_id: str | None = None,
        query: str | None = None,
        project_path: str | None = None,
    ) -> dict[str, Any]:
        sections = self._artifact_sections_for_chunking(
            artifact_type=artifact_type,
            profile=profile,
            task_id=task_id,
            project_path=project_path,
            query=query,
        )
        total_chunks = max(len(sections) // 4 + 1, 1)
        chunks = self._chunk_sections(sections, total_chunks)
        chunk_entries: list[dict[str, Any]] = []
        for idx, chunk_sections in enumerate(chunks):
            markdown, used_tokens = self._render_context_sections(chunk_sections, max_tokens=None)
            chunk_entries.append(
                {
                    "chunk_index": idx,
                    "is_last": idx == len(chunks) - 1,
                    "section_names": [section["name"] for section in chunk_sections],
                    "used_tokens": used_tokens,
                    "char_count": len(markdown),
                }
            )
        return {
            "artifact_type": artifact_type,
            "profile": profile,
            "task_id": task_id,
            "query": query,
            "total_chunks": len(chunks),
            "chunks": chunk_entries,
        }

    def generate_progressive_context(
        self,
        artifact_type: str = "context_profile",
        profile: str = "deep",
        start_chunk: int = 0,
        chunk_count: int = 2,
        task_id: str | None = None,
        query: str | None = None,
        project_path: str | None = None,
    ) -> dict[str, Any]:
        plan = self.list_context_chunks(
            artifact_type=artifact_type,
            profile=profile,
            task_id=task_id,
            query=query,
            project_path=project_path,
        )
        rendered_chunks: list[dict[str, Any]] = []
        for entry in plan["chunks"][start_chunk : start_chunk + max(chunk_count, 1)]:
            rendered = self.retrieve_context_chunk(
                artifact_type=artifact_type,
                chunk_index=entry["chunk_index"],
                profile=profile,
                task_id=task_id,
                query=query,
                project_path=project_path,
            )
            rendered_chunks.append(rendered)
        combined_markdown = "\n\n".join(chunk["markdown"].strip() for chunk in rendered_chunks if chunk.get("markdown")).strip() + "\n"
        self._record_token_usage_metric(
            operation="generate_progressive_context",
            project_path=project_path,
            estimated_output_tokens=self._estimated_tokens(combined_markdown),
            compact_output_tokens=self._estimated_tokens(combined_markdown),
            compact_chars=len(combined_markdown),
            metadata={
                "artifact_type": artifact_type,
                "profile": profile,
                "start_chunk": start_chunk,
                "chunk_count": chunk_count,
            },
        )
        return {
            "artifact_type": artifact_type,
            "profile": profile,
            "start_chunk": start_chunk,
            "chunk_count": len(rendered_chunks),
            "total_chunks": plan["total_chunks"],
            "chunks": rendered_chunks,
            "remaining_chunks": max(plan["total_chunks"] - (start_chunk + len(rendered_chunks)), 0),
            "combined_markdown": combined_markdown,
        }

    def _sync_context_artifacts(self, project_path: str | None = None) -> dict[str, str]:
        pcfg = self._project_config_for(project_path)
        context_dir = pcfg.context_path
        json_dir = pcfg.json_export_dir
        json_dir.mkdir(parents=True, exist_ok=True)
        files_written: dict[str, str] = {}

        hot = self.generate_context_profile(profile="fast", task_id=None, max_tokens=1200, project_path=str(pcfg.project_path))
        balanced = self.generate_context_profile(profile="balanced", task_id=None, max_tokens=2500, project_path=str(pcfg.project_path))
        deep = self.generate_context_profile(profile="deep", task_id=None, max_tokens=4500, include_daily_notes=True, project_path=str(pcfg.project_path))
        delta = self.generate_delta_context(project_path=str(pcfg.project_path))
        segments = self.generate_prompt_segments(profile="balanced", max_tokens=2600, project_path=str(pcfg.project_path))
        current_task = self._store(str(pcfg.project_path)).get_current_task()
        retrieval = self.generate_retrieval_context(
            query=((current_task or {}).get("title", "") or "current task"),
            task_id=(current_task or {}).get("id"),
            project_path=str(pcfg.project_path),
        )
        token_stats = self.get_token_usage_stats(limit=200, project_path=str(pcfg.project_path))

        hot_path = context_dir / "HOT_CONTEXT.md"
        balanced_path = context_dir / "BALANCED_CONTEXT.md"
        deep_path = context_dir / "DEEP_CONTEXT.md"
        delta_path = context_dir / "DELTA_CONTEXT.md"
        retrieval_path = context_dir / "RETRIEVAL_CONTEXT.md"
        stable_path = context_dir / "STABLE_CONTEXT.md"
        dynamic_path = context_dir / "DYNAMIC_CONTEXT.md"
        layers_path = json_dir / "context_layers.json"
        delta_json_path = json_dir / "delta_context.json"
        retrieval_json_path = json_dir / "retrieval_context.json"
        segments_json_path = json_dir / "prompt_segments.json"
        token_stats_path = json_dir / "token_usage_stats.json"

        write_text_atomic(hot_path, hot["markdown"])
        write_text_atomic(balanced_path, balanced["markdown"])
        write_text_atomic(deep_path, deep["markdown"])
        write_text_atomic(delta_path, delta["markdown"])
        write_text_atomic(retrieval_path, retrieval["markdown"])
        write_text_atomic(stable_path, segments["stable_markdown"])
        write_text_atomic(dynamic_path, segments["dynamic_markdown"])
        write_json_atomic(
            layers_path,
            {
                "fast": hot["metadata"],
                "balanced": balanced["metadata"],
                "deep": deep["metadata"],
            },
        )
        write_json_atomic(
            delta_json_path,
            {
                "markdown": delta["markdown"],
                "metadata": delta["metadata"],
            },
        )
        write_json_atomic(
            retrieval_json_path,
            {
                "markdown": retrieval["markdown"],
                "metadata": retrieval["metadata"],
            },
        )
        write_json_atomic(
            segments_json_path,
            {
                "stable_markdown": segments["stable_markdown"],
                "dynamic_markdown": segments["dynamic_markdown"],
                "combined_markdown": segments["combined_markdown"],
                "metadata": segments["metadata"],
            },
        )
        write_json_atomic(token_stats_path, token_stats)
        files_written["HOT_CONTEXT.md"] = str(hot_path)
        files_written["BALANCED_CONTEXT.md"] = str(balanced_path)
        files_written["DEEP_CONTEXT.md"] = str(deep_path)
        files_written["DELTA_CONTEXT.md"] = str(delta_path)
        files_written["RETRIEVAL_CONTEXT.md"] = str(retrieval_path)
        files_written["STABLE_CONTEXT.md"] = str(stable_path)
        files_written["DYNAMIC_CONTEXT.md"] = str(dynamic_path)
        files_written["context_layers.json"] = str(layers_path)
        files_written["delta_context.json"] = str(delta_json_path)
        files_written["retrieval_context.json"] = str(retrieval_json_path)
        files_written["prompt_segments.json"] = str(segments_json_path)
        files_written["token_usage_stats.json"] = str(token_stats_path)
        return files_written

    def _submit_precompute(self, project_path: str) -> None:
        """Submit a background precomputation job for context profiles, if not already running."""
        with self._precompute_lock:
            job_key = f"precompute:{project_path}"
            if job_key in self._precompute_jobs and not self._precompute_jobs[job_key].done():
                return
            self._precompute_jobs[job_key] = self._precompute_executor.submit(
                self._run_precompute, project_path
            )

    def _run_precompute(self, project_path: str) -> dict[str, Any]:
        """Background precomputation of standard context profiles and delta context."""
        try:
            store = self._store(project_path)
            task = store.get_current_task()
            if not task:
                sessions = store.get_active_sessions(limit=1)
                active_task_id = (sessions[0] if sessions else {}).get("task_id")
                if active_task_id:
                    task = store.get_task(active_task_id)
            scope_key = self._context_scope_key(task)
            state_version = store.get_context_state_version(include_semantic=True)
            profiles = [
                ("fast", None, 1200),
                ("balanced", None, 2500),
                ("deep", True, 4500),
            ]
            results: dict[str, Any] = {}
            for profile, include_daily_notes, max_toks in profiles:
                params = {
                    "profile": profile,
                    "task_id": task["id"] if task else None,
                    "max_tokens": max_toks,
                    "include_daily_notes": include_daily_notes or False,
                }
                params_signature = self._artifact_signature(params)
                result = self.generate_context_profile(
                    profile=profile,
                    task_id=task["id"] if task else None,
                    max_tokens=max_toks,
                    include_daily_notes=include_daily_notes or False,
                    project_path=project_path,
                )
                store.upsert_context_artifact(
                    artifact_key=f"precomputed:{scope_key}:{profile}",
                    artifact_type="precomputed",
                    scope_key=scope_key,
                    params_signature=params_signature,
                    state_version=state_version,
                    content=result["markdown"],
                    metadata={
                        "profile": profile,
                        "max_tokens": max_toks,
                        "precomputed": True,
                    },
                )
                results[profile] = result["markdown"][:80]
            delta_params = {"task_id": task["id"] if task else None}
            delta_result = self.generate_delta_context(
                task_id=task["id"] if task else None,
                project_path=project_path,
            )
            store.upsert_context_artifact(
                artifact_key=f"precomputed:{scope_key}:delta",
                artifact_type="precomputed_delta",
                scope_key=scope_key,
                params_signature=self._artifact_signature(delta_params),
                state_version=state_version,
                content=delta_result["markdown"],
                metadata={"precomputed": True},
            )
            results["delta"] = delta_result["markdown"][:80]
            return {"precomputed": True, "profiles": results}
        except Exception as exc:
            return {"precomputed": False, "error": str(exc)}

    def _generate_minimal_fast_context(
        self,
        task_id: str | None = None,
        project_path: str | None = None,
    ) -> dict[str, Any]:
        """Generate a lightweight L0-only fast context for startup/resume use cases.

        Returns only mission, current_task, relevant_files, latest_handoff, and blockers
        sections — no semantic lookups, dependency map, daily notes, or audit sections.
        Targeted at ~400-600 tokens.
        """
        store = self._store(project_path)
        task = store.get_task(task_id) if task_id else store.get_current_task()
        brief = store.get_project_brief()
        mission = brief.get("Mission", "No mission defined.")
        relevant_files = store.get_relevant_files(task["id"] if task else None)
        blockers = store.get_blockers(open_only=True, limit=6)
        latest_handoff = store.get_latest_handoff()
        recent_commands = store.list_command_events(limit=3, task_id=task["id"] if task else task_id)
        recent_failures = store.get_command_failures(limit=2, task_id=task["id"] if task else task_id)
        current_task_lines = ["## Current Task", ""]
        if task:
            current_task_lines.extend(
                [
                    f"- ID: {task['id']}",
                    f"- Title: {task['title']}",
                    f"- Status: {task['status']}",
                    f"- Priority: {task['priority']}",
                    "",
                    task["description"] or "No description.",
                ]
            )
        else:
            current_task_lines.append("- No current task is set.")
        sections = [
            {"name": "mission", "layer": "L0", "priority": 1, "text": f"## Mission\n{mission}"},
            {"name": "current_task", "layer": "L0", "priority": 2, "text": "\n".join(current_task_lines)},
        ]
        relevant_lines = ["## Relevant Files"]
        relevant_lines.extend([f"- {item}" for item in relevant_files] or ["- None"])
        sections.append({"name": "relevant_files", "layer": "L0", "priority": 3, "text": "\n".join(relevant_lines)})
        handoff_lines = ["## Latest Handoff"]
        if latest_handoff:
            handoff_lines.extend([
                f"- From: {latest_handoff['from_actor']}",
                f"- To: {latest_handoff['to_actor']}",
                f"- Created: {latest_handoff['created_at']}",
                "",
                latest_handoff["summary"] or "No summary.",
            ])
        else:
            handoff_lines.append("- No handoff recorded yet.")
        sections.append({"name": "latest_handoff", "layer": "L0", "priority": 4, "text": "\n".join(handoff_lines)})
        blocker_lines = ["## Open Blockers"]
        blocker_lines.extend([f"- [{item['id']}] {item['title']}: {item['description']}" for item in blockers] or ["- None"])
        sections.append({"name": "blockers", "layer": "L0", "priority": 5, "text": "\n".join(blocker_lines)})
        command_lines = ["## Recent Commands"]
        command_lines.extend(
            [
                f"- [{item['status']}] `{item['command_text']}`"
                + (f": {self._single_line_summary(item['summary'], max_chars=140)}" if item.get("summary") else "")
                for item in recent_commands
            ]
            or ["- None"]
        )
        sections.append({"name": "recent_commands", "layer": "L0", "priority": 6, "text": "\n".join(command_lines)})
        failure_lines = ["## Recent Command Failures"]
        failure_lines.extend(
            [
                f"- [exit {item.get('exit_code', 0)}] `{item['command_text']}`"
                + (f": {self._single_line_summary(item['summary'], max_chars=140)}" if item.get("summary") else "")
                for item in recent_failures
            ]
            or ["- None"]
        )
        sections.append({"name": "recent_command_failures", "layer": "L0", "priority": 7, "text": "\n".join(failure_lines)})
        markdown, used_tokens = self._render_context_sections(sections, max_tokens=None)
        return {
            "markdown": markdown.strip() + "\n",
            "used_tokens": used_tokens,
            "sections": [s["name"] for s in sections],
        }

    def list_tool_definitions(self, project_path: str | None = None) -> list[dict[str, Any]]:
        return TOOL_DEFINITIONS

    def list_resource_definitions(self, project_path: str | None = None) -> list[dict[str, Any]]:
        return [
            {
                "uri": "obsmcp://project/brief",
                "name": "Project Brief",
                "description": "Human-readable project brief composed from structured state.",
                "mimeType": "text/markdown",
            },
            {
                "uri": "obsmcp://project/current-task",
                "name": "Current Task",
                "description": "The current task JSON snapshot.",
                "mimeType": "application/json",
            },
            {
                "uri": "obsmcp://project/latest-handoff",
                "name": "Latest Handoff",
                "description": "Most recent handoff for cross-model continuity.",
                "mimeType": "text/markdown",
            },
            {
                "uri": "obsmcp://project/status-snapshot",
                "name": "Status Snapshot",
                "description": "Compact project status snapshot.",
                "mimeType": "application/json",
            },
            {
                "uri": "obsmcp://context/compact",
                "name": "Compact Context",
                "description": "Token-efficient continuity context for quick prompting.",
                "mimeType": "text/markdown",
            },
            {
                "uri": "obsmcp://context/hot",
                "name": "Hot Context",
                "description": "Fastest L0/L1 continuity context for low-latency startup.",
                "mimeType": "text/markdown",
            },
            {
                "uri": "obsmcp://context/delta",
                "name": "Delta Context",
                "description": "Only what changed since the last handoff/session reference.",
                "mimeType": "text/markdown",
            },
            {
                "uri": "obsmcp://context/retrieval",
                "name": "Retrieval Context",
                "description": "Task-scoped retrieval-first context with ranked files, work, and semantic hits.",
                "mimeType": "text/markdown",
            },
            {
                "uri": "obsmcp://context/stable",
                "name": "Stable Prompt Prefix",
                "description": "Cache-friendly stable project prompt segment.",
                "mimeType": "text/markdown",
            },
            {
                "uri": "obsmcp://context/dynamic",
                "name": "Dynamic Prompt Suffix",
                "description": "Task and delta oriented prompt segment for active work.",
                "mimeType": "text/markdown",
            },
            {
                "uri": "obsmcp://context/files",
                "name": "Context Files",
                "description": "Paths to synced .context continuity files.",
                "mimeType": "application/json",
            },
            {
                "uri": "obsmcp://sessions/active",
                "name": "Active Sessions",
                "description": "Open AI/agent sessions tracked by obsmcp.",
                "mimeType": "application/json",
            },
            {
                "uri": "obsmcp://sessions/audit",
                "name": "Session Audit",
                "description": "Missing write-back and heartbeat audit findings.",
                "mimeType": "application/json",
            },
            {
                "uri": "obsmcp://prompts/master",
                "name": "Master Prompt",
                "description": "First-chat master prompt for MCP-dependent tools and agents.",
                "mimeType": "text/markdown",
            },
            {
                "uri": "obsmcp://projects/list",
                "name": "Projects",
                "description": "Registered obsmcp projects.",
                "mimeType": "application/json",
            },
            {
                "uri": "obsmcp://context/resume",
                "name": "Resume Packet",
                "description": "Compact resume packet for cross-tool handoff and recovery.",
                "mimeType": "text/markdown",
            },
            {
                "uri": "obsmcp://context/startup",
                "name": "Startup Context",
                "description": "Delta-first startup context with recent command summaries and execution policy hints.",
                "mimeType": "text/markdown",
            },
            {
                "uri": "obsmcp://knowledge/index",
                "name": "Semantic Knowledge Index",
                "description": "Summary of semantic symbol counts for the active project.",
                "mimeType": "application/json",
            },
            {
                "uri": "obsmcp://metrics/tokens",
                "name": "Token Usage Metrics",
                "description": "Recent token, compaction, and prompt-cache usage metrics.",
                "mimeType": "application/json",
            },
        ]

    def get_resource(self, uri: str, project_path: str | None = None, project_slug: str | None = None) -> dict[str, Any]:
        pcfg = self._project_config_for(project_path, project_slug=project_slug)
        resolved_project_path = str(pcfg.project_path)
        if uri == "obsmcp://project/brief":
            brief = self.get_project_brief(project_path=resolved_project_path)
            text = "\n".join([f"# Project Brief", ""] + [f"## {section}\n\n{content}\n" for section, content in brief.items()])
            return {"uri": uri, "mimeType": "text/markdown", "text": text}
        if uri == "obsmcp://project/current-task":
            return {"uri": uri, "mimeType": "application/json", "json": self.get_current_task(project_path=resolved_project_path) or {}}
        if uri == "obsmcp://project/latest-handoff":
            handoff = self.get_latest_handoff(project_path=resolved_project_path)
            text = render_handoff_markdown(handoff)
            return {"uri": uri, "mimeType": "text/markdown", "text": text}
        if uri == "obsmcp://project/status-snapshot":
            return {"uri": uri, "mimeType": "application/json", "json": self.get_project_status_snapshot(project_path=resolved_project_path)}
        if uri == "obsmcp://context/compact":
            return {"uri": uri, "mimeType": "text/markdown", "text": self.generate_compact_context(project_path=resolved_project_path)}
        if uri == "obsmcp://context/hot":
            return {"uri": uri, "mimeType": "text/markdown", "text": self.generate_context_profile(profile="fast", max_tokens=1200, project_path=resolved_project_path)["markdown"]}
        if uri == "obsmcp://context/delta":
            return {"uri": uri, "mimeType": "text/markdown", "text": self.generate_delta_context(project_path=resolved_project_path)["markdown"]}
        if uri == "obsmcp://context/retrieval":
            current_task = self.get_current_task(project_path=resolved_project_path) or {}
            return {
                "uri": uri,
                "mimeType": "text/markdown",
                "text": self.generate_retrieval_context(
                    query=current_task.get("title", "") or "current task",
                    task_id=current_task.get("id"),
                    project_path=resolved_project_path,
                )["markdown"],
            }
        if uri == "obsmcp://context/stable":
            return {"uri": uri, "mimeType": "text/markdown", "text": self.generate_prompt_segments(project_path=resolved_project_path)["stable_markdown"]}
        if uri == "obsmcp://context/dynamic":
            return {"uri": uri, "mimeType": "text/markdown", "text": self.generate_prompt_segments(project_path=resolved_project_path)["dynamic_markdown"]}
        if uri == "obsmcp://context/files":
            paths = {
                name: str(pcfg.context_path / name)
                for name in [
                    "PROJECT_CONTEXT.md",
                    "CURRENT_TASK.json",
                    "HANDOFF.md",
                    "DECISIONS.md",
                    "RELEVANT_FILES.json",
                    "BLOCKERS.json",
                    "SESSION_SUMMARY.md",
                    "SESSION_AUDIT.json",
                    "RESUME_PACKET.md",
                    "HOT_CONTEXT.md",
                    "BALANCED_CONTEXT.md",
                    "DEEP_CONTEXT.md",
                    "DELTA_CONTEXT.md",
                    "RETRIEVAL_CONTEXT.md",
                    "STABLE_CONTEXT.md",
                    "DYNAMIC_CONTEXT.md",
                ]
            }
            return {"uri": uri, "mimeType": "application/json", "json": paths}
        if uri == "obsmcp://projects/list":
            return {"uri": uri, "mimeType": "application/json", "json": self.list_projects()}
        if uri == "obsmcp://context/resume":
            return {"uri": uri, "mimeType": "text/markdown", "text": self.generate_resume_packet(project_path=resolved_project_path)["markdown"]}
        if uri == "obsmcp://context/startup":
            return {"uri": uri, "mimeType": "text/markdown", "text": self.generate_startup_context(project_path=resolved_project_path)["markdown"]}
        if uri == "obsmcp://knowledge/index":
            self._refresh_semantic_index(project_path=resolved_project_path)
            return {"uri": uri, "mimeType": "application/json", "json": self._store(resolved_project_path).get_symbol_index_stats()}
        if uri == "obsmcp://metrics/tokens":
            return {"uri": uri, "mimeType": "application/json", "json": self.get_token_usage_stats(project_path=resolved_project_path)}
        if uri == "obsmcp://sessions/active":
            return {"uri": uri, "mimeType": "application/json", "json": self.get_active_sessions(project_path=resolved_project_path)}
        if uri == "obsmcp://sessions/audit":
            return {"uri": uri, "mimeType": "application/json", "json": self.detect_missing_writeback(project_path=resolved_project_path)}
        if uri == "obsmcp://prompts/master":
            return {"uri": uri, "mimeType": "text/markdown", "text": self.generate_startup_prompt_template()}
        raise KeyError(f"Unknown resource: {uri}")

    def health_check(self, project_path: str | None = None, project_slug: str | None = None) -> dict[str, Any]:
        if not project_path and not project_slug and not self.config.bootstrap_default_project_on_startup:
            return {
                "name": self.config.app_name,
                "description": self.config.description,
                "host": self.config.host,
                "port": self.config.port,
                "project_slug": None,
                "project_path": None,
                "workspace_root": None,
                "database_path": None,
                "context_dir": None,
                "obsidian_vault_dir": None,
                "db_exists": False,
                "port_in_use": is_port_open(self.config.host, self.config.port),
                "current_task": None,
                "active_sessions": 0,
                "audit_issue_count": 0,
                "registered_projects": len(self.registry.list_projects()),
                "default_project_path": str(self.config.default_project_path) if self.config.default_project_path else None,
                "bootstrap_default_project_on_startup": False,
                "api_version": self.API_VERSION,
                "tool_schema_version": self.TOOL_SCHEMA_VERSION,
                "compatibility_rules_version": self.COMPATIBILITY_RULES_VERSION,
            }

        pcfg = self._project_config_for(project_path, project_slug=project_slug)
        store = self._store(project_path, project_slug=project_slug)
        active_sessions = store.get_active_sessions(limit=100)
        return {
            "name": self.config.app_name,
            "description": self.config.description,
            "host": self.config.host,
            "port": self.config.port,
            "project_slug": pcfg.project_slug,
            "project_path": str(pcfg.project_path),
            "workspace_root": str(pcfg.workspace_root),
            "database_path": str(pcfg.db_path),
            "context_dir": str(pcfg.context_path),
            "obsidian_vault_dir": str(pcfg.vault_path),
            "db_exists": pcfg.db_path.exists(),
            "port_in_use": is_port_open(self.config.host, self.config.port),
            "current_task": store.get_current_task(),
            "active_sessions": len(active_sessions),
            "audit_issue_count": len(store.detect_missing_writeback()),
            "registered_projects": len(self.registry.list_projects()),
            "default_project_path": str(self.config.default_project_path) if self.config.default_project_path else None,
            "bootstrap_default_project_on_startup": self.config.bootstrap_default_project_on_startup,
            "api_version": self.API_VERSION,
            "tool_schema_version": self.TOOL_SCHEMA_VERSION,
            "compatibility_rules_version": self.COMPATIBILITY_RULES_VERSION,
        }

    def register_project(
        self,
        repo_path: str,
        name: str | None = None,
        tags: list[str] | None = None,
        project_path: str | None = None,
    ) -> dict[str, Any]:
        pcfg = self._project_config_for(repo_path)
        record = self._register_project_config(pcfg)
        if name or tags is not None:
            record = self.registry.touch(pcfg.project_slug, name=name, tags=tags, active_session_count=record.get("active_session_count", 0)) or record
        self.attach_repo_bridge(project_path=str(pcfg.project_path))
        self.sync_all(project_path=str(pcfg.project_path))
        return record

    def list_projects(self) -> list[dict[str, Any]]:
        return self.registry.list_projects()

    def resolve_project(self, project_slug: str | None = None, project_path: str | None = None) -> dict[str, Any]:
        pcfg = self._project_config_for(project_path, project_slug=project_slug)
        return {
            "project_slug": pcfg.project_slug,
            "project_name": pcfg.project_name,
            "project_path": str(pcfg.project_path),
            "workspace_root": str(pcfg.workspace_root),
            "vault_path": str(pcfg.vault_path),
            "context_path": str(pcfg.context_path),
            "db_path": str(pcfg.db_path),
            "sessions_path": str(pcfg.sessions_path),
            "manifest_path": str(pcfg.manifest_path),
        }

    def get_project_workspace_paths(self, project_slug: str | None = None, project_path: str | None = None) -> dict[str, Any]:
        return self.resolve_project(project_slug=project_slug, project_path=project_path)

    def attach_repo_bridge(self, project_path: str | None = None, project_slug: str | None = None) -> dict[str, Any]:
        pcfg = self._project_config_for(project_path, project_slug=project_slug)
        payload = {
            "project_slug": pcfg.project_slug,
            "project_name": pcfg.project_name,
            "repo_path": str(pcfg.project_path),
            "workspace_root": str(pcfg.workspace_root),
            "vault_path": str(pcfg.vault_path),
            "context_path": str(pcfg.context_path),
            "db_path": str(pcfg.db_path),
            "sessions_path": str(pcfg.sessions_path),
            "hub_vault_path": str(self.config.hub_vault_dir),
            "updated_at": utc_now(),
        }
        write_json_atomic(pcfg.bridge_file_path, payload)
        return {"attached": True, "bridge_file": str(pcfg.bridge_file_path), "workspace_root": str(pcfg.workspace_root)}

    def migrate_project_layout(self, project_path: str | None = None, project_slug: str | None = None) -> dict[str, Any]:
        pcfg = self._project_config_for(project_path, project_slug=project_slug)
        copied: list[str] = []

        legacy_context = pcfg.project_path / ".context"
        if legacy_context.exists():
            for source in legacy_context.rglob("*"):
                if not source.is_file():
                    continue
                target = pcfg.context_path / source.relative_to(legacy_context)
                if not target.exists():
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source, target)
                    copied.append(str(target))

        legacy_vault = pcfg.project_path / "obsidian" / "vault"
        if legacy_vault.exists():
            for source in legacy_vault.rglob("*"):
                if not source.is_file():
                    continue
                target = pcfg.vault_path / source.relative_to(legacy_vault)
                if not target.exists():
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source, target)
                    copied.append(str(target))

        self.attach_repo_bridge(project_path=str(pcfg.project_path))
        self.sync_all(project_path=str(pcfg.project_path))
        return {
            "migrated": True,
            "project_slug": pcfg.project_slug,
            "project_path": str(pcfg.project_path),
            "workspace_root": str(pcfg.workspace_root),
            "copied_files": copied,
            "copied_count": len(copied),
        }

    def get_project_brief(self, project_path: str | None = None) -> dict[str, str]:
        return self._store(project_path).get_project_brief()

    def get_current_task(self, project_path: str | None = None) -> dict[str, Any] | None:
        return self._store(project_path).get_current_task()

    def get_active_tasks(self, project_path: str | None = None) -> list[dict[str, Any]]:
        return self._store(project_path).get_active_tasks(limit=20)

    def set_current_task(self, task_id: str, actor: str = "unknown", session_id: str | None = None, project_path: str | None = None) -> dict[str, Any] | None:
        result = self._store(project_path).set_current_task(task_id=task_id, actor=actor, session_id=session_id)
        if result and self.config.semantic_auto_generate.on_set_current_task:
            self._submit_semantic_prewarm(
                result.get("relevant_files", []),
                task_id=result.get("id"),
                project_path=project_path,
                reason="set_current_task",
                limit=self.config.semantic_auto_generate.max_modules_per_write,
            )
        self.sync_all(project_path)
        return result

    def get_latest_handoff(self, project_path: str | None = None) -> dict[str, Any] | None:
        return self._store(project_path).get_latest_handoff()

    def get_recent_work(
        self,
        limit: int | None = None,
        after_id: int | None = None,
        project_path: str | None = None,
    ) -> list[dict[str, Any]]:
        effective_limit = max(1, min(limit or self.config.max_recent_work_items, 1000))
        return self._store(project_path).get_recent_work(limit=effective_limit, after_id=after_id)

    def get_decisions(
        self,
        limit: int | None = None,
        after_id: int | None = None,
        project_path: str | None = None,
    ) -> list[dict[str, Any]]:
        effective_limit = max(1, min(limit or self.config.max_decisions, 1000))
        return self._store(project_path).get_decisions(limit=effective_limit, after_id=after_id)

    def get_blockers(
        self,
        open_only: bool = True,
        limit: int | None = None,
        after_id: int | None = None,
        project_path: str | None = None,
    ) -> list[dict[str, Any]]:
        effective_limit = max(1, min(limit or self.config.max_blockers, 1000))
        return self._store(project_path).get_blockers(open_only=open_only, limit=effective_limit, after_id=after_id)

    def get_relevant_files(self, task_id: str | None = None, project_path: str | None = None) -> list[str]:
        return self._store(project_path).get_relevant_files(task_id=task_id)

    def get_table_schema(self, table_name: str, project_path: str | None = None) -> dict[str, Any]:
        return self._store(project_path).get_table_schema(table_name)

    def search_notes(self, query: str, limit: int = 10, project_path: str | None = None) -> list[dict[str, Any]]:
        return self._store(project_path).search_notes(query, limit=limit)

    def read_note(self, path: str, project_path: str | None = None) -> dict[str, Any]:
        return self._store(project_path).read_note(path)

    def get_project_status_snapshot(self, project_path: str | None = None) -> dict[str, Any]:
        return self._store(project_path).get_project_status_snapshot()

    def log_work(self, actor: str = "unknown", project_path: str | None = None, **kwargs: Any) -> dict[str, Any]:
        result = self._store(project_path).log_work(actor=actor, **kwargs)
        if self.config.semantic_auto_generate.on_log_work:
            self._submit_semantic_prewarm(
                result.get("files", []),
                task_id=kwargs.get("task_id"),
                project_path=project_path,
                reason="log_work",
                limit=self.config.semantic_auto_generate.max_modules_per_write,
            )
        self.sync_all(project_path)
        return result

    def update_task(self, task_id: str, actor: str = "unknown", project_path: str | None = None, **fields: Any) -> dict[str, Any]:
        store = self._store(project_path)
        result = store.update_task(task_id, actor=actor, **fields)
        if self.config.semantic_auto_generate.on_update_task:
            candidate_files = fields.get("relevant_files") if isinstance(fields.get("relevant_files"), list) else result.get("relevant_files", [])
            self._submit_semantic_prewarm(
                candidate_files,
                task_id=task_id,
                project_path=project_path,
                reason="update_task",
                limit=self.config.semantic_auto_generate.max_modules_per_write,
            )
        if fields.get("status") == "done":
            self.scan_codebase(project_path=project_path, force_refresh=False)
        try:
            self.sync_all(project_path)
        except Exception:
            # Side-effect sync may race with background scans that rebuild the semantic index.
            # The primary operation (task update) already succeeded — surface the result
            # even if sync failed. The error is non-fatal for the caller's purpose.
            pass
        return result

    def create_task(self, actor: str = "unknown", project_path: str | None = None, **kwargs: Any) -> dict[str, Any]:
        result = self._store(project_path).create_task(actor=actor, **kwargs)
        if self.config.semantic_auto_generate.on_create_task:
            self._submit_semantic_prewarm(
                result.get("relevant_files", []),
                task_id=result.get("id"),
                project_path=project_path,
                reason="create_task",
                limit=self.config.semantic_auto_generate.max_modules_per_write,
            )
        self.sync_all(project_path)
        return result

    def log_checkpoint(
        self,
        task_id: str,
        checkpoint_id: str,
        title: str,
        actor: str = "unknown",
        message: str = "",
        status: str = "completed",
        files: list[str] | None = None,
        session_id: str | None = None,
        project_path: str | None = None,
    ) -> dict[str, Any]:
        if not self.config.checkpoints.enabled:
            raise ValueError("Checkpoint logging is disabled in config.")
        result = self._store(project_path).log_checkpoint(
            task_id=task_id,
            checkpoint_id=checkpoint_id,
            title=title,
            message=message,
            status=status,
            files=files,
            actor=actor,
            session_id=session_id,
        )
        progress = self._store(project_path).get_checkpoint_progress(task_id)
        if self.config.checkpoints.auto_rollup and self.config.checkpoints.auto_close_task and progress.get("all_expected_complete"):
            task = self._store(project_path).get_task(task_id)
            if task and task.get("status") != "done":
                result["auto_closed_task"] = self._store(project_path).update_task(
                    task_id,
                    actor=actor,
                    session_id=session_id,
                    status="done",
                )
        self.sync_all(project_path)
        return result

    def get_task_progress(self, task_id: str, project_path: str | None = None) -> dict[str, Any]:
        store = self._store(project_path)
        task = store.get_task(task_id)
        if not task:
            raise ValueError(f"Unknown task: {task_id}")
        progress = store.get_checkpoint_progress(task_id)
        progress["recent_checkpoints"] = store.get_checkpoints_for_task(task_id, limit=self.config.checkpoints.render_limit)
        progress["task"] = task
        return progress

    def log_decision(self, actor: str = "unknown", project_path: str | None = None, **kwargs: Any) -> dict[str, Any]:
        result = self._store(project_path).log_decision(actor=actor, **kwargs)
        self.sync_all(project_path)
        return result

    def log_blocker(self, actor: str = "unknown", project_path: str | None = None, **kwargs: Any) -> dict[str, Any]:
        result = self._store(project_path).log_blocker(actor=actor, **kwargs)
        self.sync_all(project_path)
        return result

    def resolve_blocker(self, blocker_id: int, resolution_note: str, actor: str = "unknown", project_path: str | None = None) -> dict[str, Any] | None:
        result = self._store(project_path).resolve_blocker(blocker_id, resolution_note=resolution_note, actor=actor)
        self.sync_all(project_path)
        return result

    def _build_handoff_context(self, task_id: str | None = None, project_path: str | None = None) -> dict[str, Any]:
        store = self._store(project_path)
        task = store.get_task(task_id) if task_id else store.get_current_task()
        effective_task_id = task["id"] if task else task_id
        relevant_files = store.get_relevant_files(task_id=effective_task_id, limit=8)
        blockers = [item for item in store.get_blockers(open_only=True, limit=8) if not effective_task_id or item.get("task_id") in {None, effective_task_id}]
        decisions = [item for item in store.get_decisions(limit=8) if not effective_task_id or item.get("task_id") in {None, effective_task_id}]
        recent_work = [item for item in store.get_recent_work(limit=10) if not effective_task_id or item.get("task_id") in {None, effective_task_id}]
        semantic_suggestions = self._semantic_lookup_suggestions(relevant_files, project_path=project_path, limit=6)
        return {
            "task": task,
            "relevant_files": relevant_files,
            "blockers": blockers[:4],
            "decisions": decisions[:4],
            "recent_work": recent_work[:4],
            "semantic_suggestions": semantic_suggestions[:4],
        }

    def generate_fast_context(
        self,
        task_id: str | None = None,
        project_path: str | None = None,
    ) -> dict[str, Any]:
        """Generate a guaranteed-fast L0-only context for startup/resume use cases.

        This method bypasses the full tiered section assembly path and directly renders
        only the essential L0 sections (mission, current task, relevant files, handoff,
        blockers). It is ephemeral — no artifact is written — and returns in a single
        direct call with no cache lookup overhead.
        """
        result = self._generate_minimal_fast_context(task_id=task_id, project_path=project_path)
        result["fast"] = True
        result["ephemeral"] = True
        self._record_token_usage_metric(
            operation="generate_fast_context",
            project_path=project_path,
            estimated_output_tokens=result.get("used_tokens", self._estimated_tokens(result["markdown"])),
            compact_output_tokens=result.get("used_tokens", self._estimated_tokens(result["markdown"])),
            compact_chars=len(result["markdown"]),
            metadata={
                "sections": result.get("sections", []),
                "ephemeral": True,
            },
        )
        return result

    def _get_cached_delta_context(
        self,
        *,
        task_id: str | None = None,
        project_path: str | None = None,
    ) -> dict[str, Any] | None:
        store = self._store(project_path)
        task = store.get_task(task_id) if task_id else store.get_current_task()
        scope_key = self._context_scope_key(task)
        params_signature = self._artifact_signature({"task_id": task["id"] if task else task_id})
        cached = store.get_context_artifact("precomputed_delta", scope_key, params_signature)
        if not cached:
            return None
        state_version = store.get_context_state_version(include_semantic=True)
        if cached["state_version"] != state_version or store.is_context_stale(cached):
            return None
        return {
            "cached": True,
            "markdown": cached["content"],
            "state_version": state_version,
            "metadata": dict(cached.get("metadata") or {}),
        }

    def generate_startup_context(
        self,
        task_id: str | None = None,
        session_id: str | None = None,
        max_tokens: int = 1800,
        prefer_cached_delta: bool = True,
        project_path: str | None = None,
    ) -> dict[str, Any]:
        store = self._store(project_path)
        task = store.get_task(task_id) if task_id else store.get_current_task()
        effective_task_id = task["id"] if task else task_id
        if self.config.semantic_auto_generate.on_startup:
            self._best_effort_semantic_prewarm(
                None,
                task_id=effective_task_id,
                project_path=project_path,
                reason="startup_context",
                limit=self.config.semantic_auto_generate.max_modules_per_write,
                wait_ms=self.config.semantic_auto_generate.wait_ms_on_startup,
            )
        fast = self.generate_fast_context(task_id=effective_task_id, project_path=project_path)
        delta = self._get_cached_delta_context(task_id=effective_task_id, project_path=project_path) if prefer_cached_delta else None
        if not delta:
            delta = self.generate_delta_context(task_id=effective_task_id, project_path=project_path)
        recent_commands = self.get_recent_commands(limit=4, session_id=session_id, task_id=effective_task_id, project_path=project_path)["events"]
        recent_failures = self.get_command_failures(limit=3, session_id=session_id, task_id=effective_task_id, project_path=project_path)
        policy_hint = self.get_command_execution_policy(command="rg TODO .", task_id=effective_task_id, project_path=project_path)
        preflight = self.get_startup_preflight(task_id=effective_task_id, session_id=session_id, project_path=project_path)
        resume_board = self.get_resume_board(project_path=project_path)

        lines = [
            "# Startup Context",
            "",
            "## Startup Preflight",
            "",
            f"- Healthy: {preflight['ok']}",
            f"- Recommended Action: {preflight['recommended_action']}",
            "",
        ]
        lines.extend([f"- [{item['severity']}] {item['message']}" for item in preflight["warnings"]] or ["- No startup warnings."])
        lines.extend(
            [
                "",
                "## Resume Board",
                "",
                f"- Open Tasks: {len(resume_board['open_tasks'])}",
                f"- Paused Tasks: {len(resume_board['paused_tasks'])}",
                f"- Active Sessions: {len(resume_board['active_sessions'])}",
                f"- Stale Sessions: {len(resume_board['stale_sessions'])}",
                "",
            ]
        )
        recommended = resume_board["recommended_resume_target"]
        if recommended.get("task"):
            lines.extend(
                [
                    f"- Recommended Task: {recommended['task']['title']}",
                    f"- Recommended Session: {(recommended.get('session') or {}).get('session_label', 'none')}",
                    "",
                ]
            )
        else:
            lines.extend(["- Recommended Task: none", "", ])
        lines.extend(
            [
                "## Fast Baseline",
                "",
                fast["markdown"].strip(),
                "",
                "## Delta Since Last Reference",
                "",
                delta["markdown"].strip(),
                "",
                "## Recent Commands",
                "",
            ]
        )
        lines.extend(
            [
                f"- [{item['status']}] `{item['command_text']}`"
                + (f": {self._single_line_summary(item['summary'], max_chars=160)}" if item.get("summary") else "")
                for item in recent_commands
            ]
            or ["- None"]
        )
        lines.extend(["", "## Recent Command Failures", ""])
        lines.extend(
            [
                f"- [exit {item.get('exit_code', 0)}] `{item['command_text']}`"
                + (f": {self._single_line_summary(item['summary'], max_chars=160)}" if item.get("summary") else "")
                for item in recent_failures
            ]
            or ["- None"]
        )
        lines.extend(["", "## Execution Policy Hint", ""])
        lines.extend(
            [
                f"- Sample Command: `{policy_hint['command']}`",
                f"- Action Type: {policy_hint['action_type']}",
                f"- Risk Level: {policy_hint['risk_level']}",
                f"- Batch Eligible: {policy_hint['can_batch']}",
                f"- Review Recommended: {policy_hint['needs_model_review']}",
            ]
        )
        markdown, used_tokens = self._render_context_sections(
            [{"name": "startup_context", "layer": "startup", "priority": 1, "text": "\n".join(lines)}],
            max_tokens=max_tokens,
        )
        self._record_token_usage_metric(
            operation="generate_startup_context",
            project_path=project_path,
            estimated_output_tokens=used_tokens,
            compact_output_tokens=used_tokens,
            compact_chars=len(markdown),
            metadata={
                "task_id": effective_task_id,
                "session_id": session_id,
                "prefer_cached_delta": prefer_cached_delta,
                "delta_cached": bool(delta.get("cached")),
            },
        )
        return {
            "markdown": markdown,
            "used_tokens": used_tokens,
            "task_id": effective_task_id,
            "session_id": session_id,
            "delta_cached": bool(delta.get("cached")),
            "sections": [
                "startup_preflight",
                "resume_board",
                "fast_baseline",
                "delta",
                "recent_commands",
                "recent_command_failures",
                "execution_policy_hint",
            ],
        }

    def retrieve_context_chunk(
        self,
        artifact_type: str = "context_profile",
        chunk_index: int = 0,
        profile: str = "deep",
        task_id: str | None = None,
        query: str | None = None,
        project_path: str | None = None,
    ) -> dict[str, Any]:
        """Retrieve a specific chunk of a context artifact.

        If the chunk does not exist in the cache, generates the full artifact,
        splits it into chunks, and stores each chunk for future requests.
        """
        store = self._store(project_path)
        task = store.get_task(task_id) if task_id else store.get_current_task()
        scope_key = self._context_scope_key(task)

        params = {
            "artifact_type": artifact_type,
            "profile": profile,
            "task_id": task["id"] if task else None,
            "query": query,
        }
        params_signature = self._artifact_signature(params)
        cached = store.get_context_chunk(scope_key, params_signature, chunk_index)
        if cached:
            metadata = cached.get("metadata") or {}
            result = {
                "chunk_index": metadata.get("chunk_index", chunk_index),
                "total_chunks": metadata.get("total_chunks", 1),
                "is_last": metadata.get("is_last", chunk_index == 0),
                "markdown": cached["content"],
                "cached": True,
                "scope_key": scope_key,
                "section_names": metadata.get("section_names", []),
                "next_chunk_index": metadata.get("chunk_index", chunk_index) + 1 if not metadata.get("is_last", chunk_index == 0) else None,
                "previous_chunk_index": max(metadata.get("chunk_index", chunk_index) - 1, 0) if metadata.get("chunk_index", chunk_index) > 0 else None,
            }
            self._record_token_usage_metric(
                operation="retrieve_context_chunk",
                project_path=project_path,
                estimated_output_tokens=self._estimated_tokens(cached["content"]),
                compact_output_tokens=self._estimated_tokens(cached["content"]),
                compact_chars=len(cached["content"]),
                metadata={
                    "artifact_type": artifact_type,
                    "cached": True,
                    "chunk_index": result["chunk_index"],
                    "total_chunks": result["total_chunks"],
                    "profile": profile,
                },
            )
            return result

        sections = self._artifact_sections_for_chunking(
            artifact_type=artifact_type,
            profile=profile,
            task_id=task["id"] if task else None,
            project_path=project_path,
            query=query,
        )

        state_version = store.get_context_state_version(include_semantic=True)
        total_chunks = max(len(sections) // 4 + 1, 1)
        chunks = self._chunk_sections(sections, total_chunks)
        for i, chunk_sections in enumerate(chunks):
            chunk_markdown, _ = self._render_context_sections(chunk_sections, max_tokens=None)
            store.upsert_context_chunk(
                scope_key=scope_key,
                params_signature=params_signature,
                chunk_index=i,
                total_chunks=total_chunks,
                state_version=state_version,
                content=chunk_markdown,
                metadata={
                    "artifact_type": artifact_type,
                    "profile": profile,
                    "query": query,
                    "section_names": [section["name"] for section in chunk_sections],
                },
            )

        target = chunks[chunk_index] if chunk_index < len(chunks) else []
        chunk_markdown, used_tokens = self._render_context_sections(target, max_tokens=None)
        result = {
            "chunk_index": chunk_index,
            "total_chunks": total_chunks,
            "is_last": chunk_index >= total_chunks - 1,
            "markdown": chunk_markdown,
            "used_tokens": used_tokens,
            "cached": False,
            "scope_key": scope_key,
            "section_names": [section["name"] for section in target],
            "next_chunk_index": chunk_index + 1 if chunk_index < total_chunks - 1 else None,
            "previous_chunk_index": chunk_index - 1 if chunk_index > 0 else None,
        }
        self._record_token_usage_metric(
            operation="retrieve_context_chunk",
            project_path=project_path,
            estimated_output_tokens=used_tokens,
            compact_output_tokens=used_tokens,
            compact_chars=len(chunk_markdown),
            metadata={
                "artifact_type": artifact_type,
                "cached": False,
                "chunk_index": chunk_index,
                "total_chunks": total_chunks,
                "profile": profile,
                "query": query,
                "section_names": result["section_names"],
            },
        )
        return result

    def _autofill_handoff_fields(
        self,
        *,
        summary: str = "",
        next_steps: str = "",
        open_questions: str = "",
        note: str = "",
        task_id: str | None = None,
        project_path: str | None = None,
    ) -> dict[str, Any]:
        store = self._store(project_path)
        context = self._build_handoff_context(task_id=task_id, project_path=project_path)
        task = context["task"]
        effective_summary = summary.strip()
        if not effective_summary:
            if task:
                status_text = f"{task['title']} is currently {task['status']}."
                if context["recent_work"]:
                    effective_summary = f"{status_text} Latest progress: {context['recent_work'][0]['message']}"
                else:
                    effective_summary = status_text
            elif context["recent_work"]:
                effective_summary = context["recent_work"][0]["message"]
            else:
                effective_summary = "Session ended without a detailed handoff summary; resume from the latest task state."

        effective_next_steps = next_steps.strip()
        if not effective_next_steps:
            if context["blockers"]:
                effective_next_steps = f"Resolve blocker: {context['blockers'][0]['title']}."
            elif task and context["relevant_files"]:
                effective_next_steps = f"Continue `{task['title']}` starting with `{Path(context['relevant_files'][0]).name}`."
            elif task:
                effective_next_steps = f"Continue `{task['title']}` from the current persisted state."
            else:
                effective_next_steps = "Review the current task, recent work, and relevant files, then continue implementation."

        effective_open_questions = open_questions.strip()
        if not effective_open_questions:
            if context["blockers"]:
                effective_open_questions = "; ".join(item["title"] for item in context["blockers"])
            else:
                effective_open_questions = "None recorded."

        note_parts = [note.strip()] if note.strip() else []
        if task:
            note_parts.append(
                "\n".join(
                    [
                        "Task State:",
                        f"- ID: {task['id']}",
                        f"- Status: {task['status']}",
                        f"- Priority: {task['priority']}",
                    ]
                )
            )
        if context["relevant_files"]:
            note_parts.append("Relevant files:\n" + "\n".join(f"- {item}" for item in context["relevant_files"]))
        if task and self.config.checkpoints.enabled:
            progress = store.get_checkpoint_progress(task["id"])
            progress_lines = [
                (
                    f"- Progress: {progress['completed_count']}/{progress['total_count']}"
                    if progress.get("total_count") is not None
                    else f"- Completed checkpoints: {progress['completed_count']}"
                )
            ]
            progress_lines.extend(
                (
                    f"- {item['phase_key']}: {item['completed_count']}/{item['total_count']} complete"
                    if item.get("total_count") is not None
                    else f"- {item['phase_key']}: {item['completed_count']} complete"
                )
                for item in progress.get("phase_rollups", [])
            )
            note_parts.append("Checkpoint progress:\n" + "\n".join(progress_lines))
        if context["decisions"]:
            note_parts.append("Recent decisions:\n" + "\n".join(f"- {item['title']}" for item in context["decisions"]))
        if context["semantic_suggestions"]:
            note_parts.append(
                "Recommended semantic lookups:\n"
                + "\n".join(
                    f"- {item['entity_key']} ({item['entity_type']}) {item['summary_hint'] or item['name']}"
                    for item in context["semantic_suggestions"]
                )
            )
        effective_note = "\n\n".join(part for part in note_parts if part).strip()
        return {
            "summary": effective_summary,
            "next_steps": effective_next_steps,
            "open_questions": effective_open_questions,
            "note": effective_note,
            "context": context,
        }

    def _inject_handoff_environment_context(
        self,
        session_id: str | None,
        project_path: str | None,
    ) -> dict[str, Any]:
        """Enrich handoff with environment-specific context from the session's IDE metadata."""
        store = self._store(project_path)
        env_info = store.get_session_env_info(session_id) if session_id else None
        lineage_chain: list[dict[str, Any]] = []
        if session_id:
            lineage_chain = store.get_session_lineage_chain(session_id)
        return {
            "env_info": env_info,
            "lineage_chain": lineage_chain,
        }

    def create_handoff(self, from_actor: str = "unknown", project_path: str | None = None, **kwargs: Any) -> dict[str, Any]:
        session_id = kwargs.get("session_id")
        env_context = self._inject_handoff_environment_context(session_id, project_path)
        enriched = self._autofill_handoff_fields(
            summary=kwargs.get("summary", ""),
            next_steps=kwargs.get("next_steps", ""),
            open_questions=kwargs.get("open_questions", ""),
            note=kwargs.get("note", ""),
            task_id=kwargs.get("task_id"),
            project_path=project_path,
        )
        payload = dict(kwargs)
        payload.update(
            {
                "summary": enriched["summary"],
                "next_steps": enriched["next_steps"],
                "open_questions": enriched["open_questions"],
                "note": enriched["note"],
            }
        )
        handoff_task_id = payload.get("task_id") or (enriched["context"].get("task") or {}).get("id")
        if self.config.semantic_auto_generate.on_handoff:
            self._best_effort_semantic_prewarm(
                enriched["context"].get("relevant_files", []),
                task_id=handoff_task_id,
                project_path=project_path,
                reason="handoff",
                limit=self.config.semantic_auto_generate.max_modules_per_write,
                wait_ms=self.config.semantic_auto_generate.wait_ms_on_handoff,
            )
        result = self._store(project_path).create_handoff(from_actor=from_actor, **payload)

        handoff_id_value = result.get("id")
        if isinstance(handoff_id_value, int) and handoff_id_value > 0:
            for tool in kwargs.get("target_tools", ["claude-code", "vscode", "jetbrains"]):
                try:
                    self.generate_cross_tool_handoff(
                        handoff_id=handoff_id_value,
                        session_id=session_id,
                        target_tool=tool,
                        target_env=kwargs.get("target_env", "default"),
                        project_path=project_path,
                    )
                except Exception:
                    pass

        self.sync_all(project_path)
        return result

    def append_handoff_note(self, handoff_id: int, note: str, actor: str = "unknown", project_path: str | None = None) -> dict[str, Any] | None:
        result = self._store(project_path).append_handoff_note(handoff_id, note=note, actor=actor)
        self.sync_all(project_path)
        return result

    def update_project_brief_section(
        self,
        section: str,
        content: str,
        actor: str = "unknown",
        session_id: str | None = None,
        project_path: str | None = None,
    ) -> dict[str, Any]:
        result = self._store(project_path).update_project_brief_section(section=section, content=content, actor=actor, session_id=session_id)
        self.sync_all(project_path)
        return result

    def create_daily_note_entry(
        self,
        entry: str,
        actor: str = "unknown",
        note_date: str | None = None,
        session_id: str | None = None,
        project_path: str | None = None,
    ) -> dict[str, Any]:
        result = self._store(project_path).create_daily_note_entry(entry=entry, actor=actor, note_date=note_date, session_id=session_id)
        self.sync_all(project_path)
        return result

    def sync_context(self, project_path: str | None = None) -> dict[str, Any]:
        return self.sync_all(project_path)

    def generate_compact_context(self, task_id: str | None = None, project_path: str | None = None) -> str:
        if self.config.semantic_auto_generate.on_startup:
            self._best_effort_semantic_prewarm(
                None,
                task_id=task_id,
                project_path=project_path,
                reason="compact_context",
                limit=self.config.semantic_auto_generate.max_modules_per_write,
                wait_ms=self.config.semantic_auto_generate.wait_ms_on_startup,
            )
        result = self.generate_context_profile(
            profile="balanced",
            task_id=task_id,
            max_tokens=2200,
            project_path=project_path,
        )
        markdown = result["markdown"]
        if markdown.startswith("# Balanced Context"):
            markdown = markdown.replace("# Balanced Context", "# Compact Context", 1)
        return markdown

    def generate_compact_context_v2(
        self,
        task_id: str | None = None,
        max_tokens: int = 3000,
        include_decision_chain: bool = True,
        include_dependency_map: bool = True,
        include_session_info: bool = True,
        include_recent_work: bool = True,
        include_daily_notes: bool = False,
        project_path: str | None = None,
    ) -> str:
        if self.config.semantic_auto_generate.on_startup:
            self._best_effort_semantic_prewarm(
                None,
                task_id=task_id,
                project_path=project_path,
                reason="compact_context_v2",
                limit=self.config.semantic_auto_generate.max_modules_per_write,
                wait_ms=self.config.semantic_auto_generate.wait_ms_on_startup,
            )
        result = self.generate_context_profile(
            profile="deep",
            task_id=task_id,
            max_tokens=max_tokens,
            include_daily_notes=include_daily_notes,
            project_path=project_path,
        )
        markdown = result["markdown"].rstrip()
        if include_decision_chain is False or include_dependency_map is False or include_session_info is False or include_recent_work is False:
            sections, _ = self._build_tiered_sections(
                "deep",
                task_id=task_id,
                project_path=project_path,
                include_daily_notes=include_daily_notes,
                include_dependency_map=include_dependency_map,
                include_session_info=include_session_info,
                include_recent_work=include_recent_work,
            )
            if not include_decision_chain:
                sections = [section for section in sections if section["name"] != "decisions"]
            markdown, used_tokens = self._render_context_sections(sections, max_tokens=max_tokens)
            markdown = markdown.rstrip() + f"\n\n---\n_Context v2 | {used_tokens} tokens (budget: {max_tokens})_\n"
            return markdown
        used_tokens = result["metadata"].get("used_tokens", self._estimated_tokens(markdown))
        return markdown + f"\n\n---\n_Context v2 | {used_tokens} tokens (budget: {max_tokens})_\n"

    def _now(self) -> datetime:
        return datetime.now(timezone.utc)

    def generate_task_snapshot(self, task_id: str | None = None, project_path: str | None = None) -> dict[str, Any]:
        store = self._store(project_path)
        task = store.get_task(task_id) if task_id else store.get_current_task()
        if not task:
            return {"task": None, "recent_work": [], "blockers": [], "relevant_files": []}
        related_logs = [item for item in store.get_recent_work(limit=100) if item.get("task_id") == task["id"]][:10]
        related_blockers = [item for item in store.get_blockers(open_only=True, limit=100) if item.get("task_id") == task["id"]]
        return {
            "task": task,
            "progress": store.get_checkpoint_progress(task["id"]),
            "recent_work": related_logs,
            "blockers": related_blockers,
            "relevant_files": store.get_relevant_files(task_id=task["id"]),
        }

    def session_open(
        self,
        actor: str,
        project_path: str | None = None,
        resume_strategy: str = "auto",
        resume_session_id: str | None = None,
        sync_mode: str = "deferred",
        **kwargs: Any,
    ) -> dict[str, Any]:
        with span("session.open", actor=actor, project_path=project_path, resume_strategy=resume_strategy):
            effective_config = self._project_config_for(project_path)
            effective_project_path = str(effective_config.project_path)
            store = self._store(project_path)
            normalized_client_name = self._normalize_client_name(kwargs.get("client_name", ""))
            normalized_model_name = self._normalize_model_name(kwargs.get("model_name", ""))
            explicit_workstream_key = (kwargs.get("workstream_key", "") or "").strip()
            task = store.get_task(kwargs.get("task_id")) if kwargs.get("task_id") else store.get_current_task()
            identity = self._derive_session_identity(
                initial_request=kwargs.get("initial_request", ""),
                session_goal=kwargs.get("session_goal", ""),
                task=task,
                session_label=kwargs.get("session_label", ""),
                workstream_key=kwargs.get("workstream_key", ""),
                workstream_title=kwargs.get("workstream_title", ""),
            )
            warnings = self._build_session_open_warnings(
                task=task,
                task_id=kwargs.get("task_id"),
                initial_request=kwargs.get("initial_request", ""),
                session_goal=kwargs.get("session_goal", ""),
                latest_handoff=store.get_latest_handoff(),
            )

            resumed: dict[str, Any] | None = None
            resume_blocked_reason: str | None = None
            if resume_strategy != "new":
                if resume_session_id:
                    resumed = store.resume_session(
                        resume_session_id,
                        actor=actor,
                        client_name=normalized_client_name,
                        model_name=normalized_model_name,
                        session_label=identity["session_label"],
                        workstream_key=identity["workstream_key"],
                        workstream_title=identity["workstream_title"],
                        task_id=kwargs.get("task_id"),
                        initial_request=kwargs.get("initial_request", ""),
                        session_goal=kwargs.get("session_goal", ""),
                    )
                elif resume_strategy == "auto":
                    existing = store.find_resumable_session(
                        actor=actor,
                        client_name=normalized_client_name,
                        model_name=normalized_model_name,
                        workstream_key=explicit_workstream_key,
                        project_path=effective_project_path,
                    )
                    if existing:
                        resume_blocked_reason = self._session_resume_mismatch_reason(
                            existing,
                            task_id=kwargs.get("task_id"),
                            initial_request=kwargs.get("initial_request", ""),
                            session_goal=kwargs.get("session_goal", ""),
                            session_label=identity["session_label"],
                            workstream_key=explicit_workstream_key,
                        )
                        if resume_blocked_reason is None:
                            resumed = store.resume_session(
                                existing["id"],
                                actor=actor,
                                client_name=normalized_client_name,
                                model_name=normalized_model_name,
                                session_label=identity["session_label"],
                                workstream_key=identity["workstream_key"],
                                workstream_title=identity["workstream_title"],
                                task_id=kwargs.get("task_id"),
                                initial_request=kwargs.get("initial_request", ""),
                                session_goal=kwargs.get("session_goal", ""),
                            )
                        else:
                            warnings.append(f"Auto-resume skipped: {resume_blocked_reason}.")

            if resumed:
                resumed["sync"] = self._sync_after_write(project_path, sync_mode=sync_mode)
                resumed["warnings"] = warnings
                return {**resumed, "resumed": True, "resume_strategy": resume_strategy}

            result = store.open_session(
                actor=actor,
                project_path=effective_project_path,
                client_name=normalized_client_name,
                model_name=normalized_model_name,
                session_label=identity["session_label"],
                workstream_key=identity["workstream_key"],
                workstream_title=identity["workstream_title"],
                initial_request=kwargs.get("initial_request", ""),
                session_goal=kwargs.get("session_goal", ""),
                task_id=kwargs.get("task_id"),
                require_heartbeat=kwargs.get("require_heartbeat", True),
                require_work_log=kwargs.get("require_work_log", True),
                heartbeat_interval_seconds=kwargs.get("heartbeat_interval_seconds", 900),
                work_log_interval_seconds=kwargs.get("work_log_interval_seconds", 1800),
                min_work_logs=kwargs.get("min_work_logs", 1),
                handoff_required=kwargs.get("handoff_required", True),
                ide_name=kwargs.get("ide_name", ""),
                ide_version=kwargs.get("ide_version", ""),
                ide_platform=kwargs.get("ide_platform", ""),
                os_name=kwargs.get("os_name", ""),
                os_version=kwargs.get("os_version", ""),
            )
            result["sync"] = self._sync_after_write(project_path, sync_mode=sync_mode)
            result["warnings"] = warnings
            return result

    def session_heartbeat(self, session_id: str, actor: str, project_path: str | None = None, sync_mode: str = "deferred", **kwargs: Any) -> dict[str, Any] | None:
        result = self._store(project_path).heartbeat_session(session_id=session_id, actor=actor, **kwargs)
        if result is not None:
            result["sync"] = self._sync_after_write(project_path, sync_mode=sync_mode)
        return result

    def session_close(self, session_id: str, actor: str, project_path: str | None = None, **kwargs: Any) -> dict[str, Any] | None:
        session = self._store(project_path).get_session(session_id)
        created_handoff: dict[str, Any] | None = None
        if kwargs.get("create_handoff", True):
            created_handoff = self.create_handoff(
                from_actor=actor,
                project_path=project_path,
                summary=kwargs.get("handoff_summary") or kwargs.get("summary", ""),
                next_steps=kwargs.get("handoff_next_steps", ""),
                open_questions=kwargs.get("handoff_open_questions", ""),
                note=kwargs.get("handoff_note", ""),
                task_id=(session or {}).get("task_id"),
                to_actor=kwargs.get("handoff_to_actor", "next-agent"),
                session_id=session_id,
            )
        elif self.config.semantic_auto_generate.on_handoff:
            self._best_effort_semantic_prewarm(
                None,
                task_id=(session or {}).get("task_id"),
                project_path=project_path,
                reason="session_close",
                limit=self.config.semantic_auto_generate.max_modules_per_write,
                wait_ms=self.config.semantic_auto_generate.wait_ms_on_handoff,
            )

        result = self._store(project_path).close_session(
            session_id=session_id,
            actor=actor,
            summary=kwargs.get("summary") or (created_handoff or {}).get("summary", ""),
            create_handoff=False,
            existing_handoff_id=(created_handoff or {}).get("id"),
            handoff_to_actor=kwargs.get("handoff_to_actor", "next-agent"),
        )
        self.sync_all(project_path)
        return result

    def get_active_sessions(
        self,
        limit: int = 50,
        after_heartbeat_at: str | None = None,
        after_id: str | None = None,
        project_path: str | None = None,
    ) -> dict[str, Any]:
        sessions = self._store(project_path).get_active_sessions(
            limit=limit,
            after_heartbeat_at=after_heartbeat_at,
            after_id=after_id,
        )
        return {
            "sessions": sessions,
            "has_more": len(sessions) == limit,
            "next_cursor": {"heartbeat_at": sessions[-1]["heartbeat_at"], "id": sessions[-1]["id"]} if sessions else None,
        }

    def detect_missing_writeback(self, include_closed: bool = False, project_path: str | None = None) -> list[dict[str, Any]]:
        return self._store(project_path).detect_missing_writeback(include_closed=include_closed)

    def get_startup_preflight(
        self,
        actor: str = "",
        task_id: str | None = None,
        session_id: str | None = None,
        initial_request: str = "",
        session_goal: str = "",
        session_label: str = "",
        workstream_key: str = "",
        client_name: str = "",
        model_name: str = "",
        project_path: str | None = None,
    ) -> dict[str, Any]:
        store = self._store(project_path)
        task = store.get_task(task_id) if task_id else store.get_current_task()
        effective_task_id = task["id"] if task else task_id
        explicit_workstream_key = (workstream_key or "").strip()
        identity = self._derive_session_identity(
            initial_request=initial_request,
            session_goal=session_goal,
            task=task,
            session_label=session_label,
            workstream_key=workstream_key,
            workstream_title="",
        )
        warnings: list[dict[str, str]] = []
        current_task = store.get_current_task()
        latest_handoff = store.get_latest_handoff()
        active_sessions = store.get_active_sessions(limit=10)
        session_audit = store.detect_missing_writeback()

        if current_task and current_task.get("status") == "done":
            warnings.append(
                {
                    "code": "current_task_done",
                    "severity": "high",
                    "message": f"Current task `{current_task['title']}` is already marked done.",
                }
            )
        if latest_handoff and effective_task_id and latest_handoff.get("task_id") and latest_handoff.get("task_id") != effective_task_id:
            warnings.append(
                {
                    "code": "latest_handoff_task_mismatch",
                    "severity": "medium",
                    "message": "Latest handoff belongs to another task.",
                }
            )
        if not task_id and self._request_needs_task_anchor(initial_request, session_goal):
            warnings.append(
                {
                    "code": "session_without_task",
                    "severity": "medium",
                    "message": "This startup request looks substantial but has no task attached.",
                }
            )

        normalized_client_name = self._normalize_client_name(client_name)
        normalized_model_name = self._normalize_model_name(model_name)
        candidate = None
        for session in active_sessions:
            if actor and session.get("actor") != actor:
                continue
            if normalized_client_name and session.get("client_name") != normalized_client_name:
                continue
            if normalized_model_name and session.get("model_name") != normalized_model_name:
                continue
            if explicit_workstream_key and session.get("workstream_key") != explicit_workstream_key:
                continue
            candidate = session
            break
        if candidate:
            mismatch = self._session_resume_mismatch_reason(
                candidate,
                task_id=effective_task_id,
                initial_request=initial_request,
                session_goal=session_goal,
                session_label=identity["session_label"],
                workstream_key=identity["workstream_key"],
            )
            if mismatch:
                warnings.append(
                    {
                        "code": "resume_mismatch",
                        "severity": "high",
                        "message": f"Auto-resume candidate exists but conflicts with the incoming request: {mismatch}.",
                    }
                )

        for issue in session_audit:
            warnings.append(
                {
                    "code": issue["issue"],
                    "severity": issue["severity"],
                    "message": issue["details"],
                }
            )

        warning_codes = {item["code"] for item in warnings}
        if "resume_mismatch" in warning_codes or "current_task_done" in warning_codes:
            recommended_action = "Create or select the correct task, then open a new session instead of auto-resuming."
        elif "session_without_task" in warning_codes:
            recommended_action = "Create/select a task before starting substantive work."
        elif any(code in warning_codes for code in {"stale_open_session", "abandoned_session", "heartbeat_overdue"}):
            recommended_action = "Recover or close stale sessions before continuing."
        else:
            recommended_action = "Startup state looks healthy."

        return {
            "ok": not any(item["severity"] == "high" for item in warnings),
            "project_path": str(self._project_config_for(project_path).project_path),
            "current_task": current_task,
            "task": task,
            "latest_handoff": latest_handoff,
            "active_session_count": len(active_sessions),
            "warnings": warnings,
            "recommended_action": recommended_action,
            "derived_session_identity": identity,
            "session_id": session_id,
        }

    def get_resume_board(self, project_path: str | None = None) -> dict[str, Any]:
        store = self._store(project_path)
        current_task = store.get_current_task()
        active_tasks = store.get_active_tasks(limit=20)
        active_sessions = store.get_active_sessions(limit=20)
        stale_issues = store.detect_missing_writeback()
        recent_handoffs = store.get_recent_handoffs(limit=5)
        active_task_ids = {item.get("task_id") for item in active_sessions if item.get("task_id")}
        paused_tasks = [task for task in active_tasks if task["id"] not in active_task_ids]
        stale_session_ids = {item["session_id"] for item in stale_issues}
        stale_sessions = [session for session in active_sessions if session["id"] in stale_session_ids]

        recommended_task = None
        if current_task and current_task.get("status") in {"open", "in_progress", "blocked"}:
            recommended_task = current_task
        elif paused_tasks:
            recommended_task = paused_tasks[0]
        elif active_tasks:
            recommended_task = active_tasks[0]

        recommended_session = None
        if recommended_task:
            for session in active_sessions:
                if session.get("task_id") == recommended_task["id"]:
                    recommended_session = session
                    break

        return {
            "current_task": current_task,
            "open_tasks": active_tasks,
            "paused_tasks": paused_tasks,
            "active_sessions": active_sessions,
            "stale_sessions": stale_sessions,
            "latest_handoffs": recent_handoffs,
            "recommended_resume_target": {
                "task": recommended_task,
                "session": recommended_session,
            },
        }

    def get_server_capabilities(self, project_path: str | None = None) -> dict[str, Any]:
        return {
            "api_version": self.API_VERSION,
            "tool_schema_version": self.TOOL_SCHEMA_VERSION,
            "compatibility_rules_version": self.COMPATIBILITY_RULES_VERSION,
            "project_path": str(self._project_config_for(project_path).project_path) if project_path else None,
            "features": {
                "session_labels": True,
                "workstreams": True,
                "startup_preflight": True,
                "resume_board": True,
                "session_mismatch_guard": True,
                "task_first_session_warnings": True,
                "reset_verification": True,
                "stable_client_identity_normalization": True,
            },
        }

    def check_client_compatibility(
        self,
        client_api_version: str = "",
        client_tool_schema_version: int | None = None,
        client_name: str = "",
        model_name: str = "",
        project_path: str | None = None,
    ) -> dict[str, Any]:
        warnings: list[str] = []
        compatible = True
        if client_api_version and client_api_version != self.API_VERSION:
            compatible = False
            warnings.append(f"Client API version `{client_api_version}` does not match server API version `{self.API_VERSION}`.")
        if client_tool_schema_version is not None and client_tool_schema_version != self.TOOL_SCHEMA_VERSION:
            compatible = False
            warnings.append(
                f"Client tool schema version `{client_tool_schema_version}` does not match server tool schema version `{self.TOOL_SCHEMA_VERSION}`."
            )
        return {
            "compatible": compatible,
            "server": self.get_server_capabilities(project_path=project_path),
            "client": {
                "client_name": self._normalize_client_name(client_name),
                "model_name": self._normalize_model_name(model_name),
                "api_version": client_api_version or None,
                "tool_schema_version": client_tool_schema_version,
            },
            "warnings": warnings,
        }

    def generate_resume_packet(
        self,
        session_id: str | None = None,
        task_id: str | None = None,
        project_path: str | None = None,
        write_files: bool = True,
    ) -> dict[str, Any]:
        inferred_path = project_path or self._find_project_path_for_session(session_id) or self._find_project_path_for_task(task_id)
        pcfg = self._project_config_for(inferred_path)
        store = self._store(inferred_path)
        task = store.get_task(task_id) if task_id else store.get_current_task()
        effective_task_id = task["id"] if task else task_id
        if self.config.semantic_auto_generate.on_startup:
            self._best_effort_semantic_prewarm(
                None,
                task_id=effective_task_id,
                project_path=inferred_path,
                reason="resume_packet",
                limit=self.config.semantic_auto_generate.max_modules_per_write,
                wait_ms=self.config.semantic_auto_generate.wait_ms_on_startup,
            )
        latest_handoff = store.get_latest_handoff()
        blockers = store.get_blockers(open_only=True, limit=self.config.max_blockers)
        decisions = store.get_decisions(limit=min(self.config.max_decisions, 8))
        recent_work = store.get_recent_work(limit=min(self.config.max_recent_work_items, 8))
        relevant_files = store.get_relevant_files(task_id=effective_task_id)
        recent_commands = store.list_command_events(limit=6, task_id=effective_task_id)
        recent_failures = store.get_command_failures(limit=4, task_id=effective_task_id)
        active_sessions = store.get_active_sessions(limit=10)
        semantic_suggestions = self._semantic_lookup_suggestions(relevant_files, project_path=inferred_path, limit=6)
        delta = self.generate_delta_context(task_id=effective_task_id, project_path=inferred_path)
        retrieval = self.generate_retrieval_context(
            query=((task or {}).get("title", "") or "current task"),
            task_id=effective_task_id,
            max_tokens=900,
            project_path=inferred_path,
        )
        lines = [
            "# Resume Packet",
            "",
            f"- Project: {pcfg.project_name}",
            f"- Project Slug: {pcfg.project_slug}",
            f"- Repo Path: {pcfg.project_path}",
            f"- Generated At: {utc_now()}",
            "",
            "## Current Task",
            "",
        ]
        if task:
            task_progress = store.get_checkpoint_progress(task["id"])
            lines.extend(
                [
                    f"- ID: {task['id']}",
                    f"- Title: {task['title']}",
                    f"- Status: {task['status']}",
                    f"- Priority: {task['priority']}",
                    (
                        f"- Checkpoints: {task_progress['completed_count']}/{task_progress['total_count']}"
                        if task_progress.get("total_count") is not None
                        else f"- Checkpoints Completed: {task_progress['completed_count']}"
                    ),
                    "",
                    task["description"],
                    "",
                ]
            )
        else:
            lines.extend(["No current task set.", ""])
        lines.extend(["## Relevant Files", ""])
        lines.extend([f"- {item}" for item in relevant_files] or ["- None"])
        lines.extend(["", "## Recommended Semantic Lookups", ""])
        lines.extend([f"- {item['entity_key']}: {item['summary_hint'] or item['name']}" for item in semantic_suggestions] or ["- None"])
        lines.extend(["", "## Delta Summary", ""])
        delta_meta = delta.get("metadata", {})
        lines.extend(
            [
                f"- Reference Kind: {delta_meta.get('reference_kind', 'unknown')}",
                f"- Since: {delta_meta.get('reference_time', 'unknown')}",
                f"- Changed Tasks: {delta_meta.get('counts', {}).get('tasks', 0)}",
                f"- New Work Logs: {delta_meta.get('counts', {}).get('work_logs', 0)}",
                f"- Decision Changes: {delta_meta.get('counts', {}).get('decisions', 0)}",
            ]
        )
        retrieval_meta = retrieval.get("metadata", {})
        lines.extend(["", "## Targeted Retrieval", ""])
        lines.extend([f"- Query: {retrieval.get('query', 'current task')}"])
        lines.extend([f"- Matched Files: {', '.join(retrieval_meta.get('matched_files', [])[:5]) or 'None'}"])
        lines.extend(
            [
                f"- Semantic Hits: {', '.join(retrieval_meta.get('semantic_entity_keys', [])[:4]) or 'None'}",
                f"- Ranked Work IDs: {', '.join(str(item) for item in retrieval_meta.get('matched_work_ids', [])[:4]) or 'None'}",
            ]
        )
        lines.extend(["", "## Recent Work", ""])
        lines.extend([f"- {item['created_at']} [{item['actor']}] {item['message']}" for item in recent_work] or ["- None"])
        lines.extend(["", "## Recent Commands", ""])
        lines.extend(
            [
                f"- {item['created_at']} [{item['status']}] `{item['command_text']}`"
                + (f": {self._single_line_summary(item['summary'], max_chars=180)}" if item.get("summary") else "")
                for item in recent_commands
            ]
            or ["- None"]
        )
        lines.extend(["", "## Recent Command Failures", ""])
        lines.extend(
            [
                f"- [exit {item.get('exit_code', 0)}] `{item['command_text']}`"
                + (f": {self._single_line_summary(item['summary'], max_chars=180)}" if item.get("summary") else "")
                for item in recent_failures
            ]
            or ["- None"]
        )
        lines.extend(["", "## Blockers", ""])
        lines.extend([f"- [{item['id']}] {item['title']}: {item['description']}" for item in blockers] or ["- None"])
        lines.extend(["", "## Decisions", ""])
        lines.extend([f"- [{item['id']}] {item['title']}: {item['decision']}" for item in decisions] or ["- None"])
        lines.extend(["", "## Latest Handoff", ""])
        if latest_handoff:
            lines.extend(
                [
                    f"- From: {latest_handoff['from_actor']}",
                    f"- To: {latest_handoff['to_actor']}",
                    "",
                    latest_handoff["summary"],
                    "",
                    f"Next Steps: {latest_handoff['next_steps'] or 'None recorded.'}",
                    "",
                ]
            )
        else:
            lines.extend(["No handoff recorded yet.", ""])
        lines.extend(["## Active Sessions", ""])
        lines.extend([f"- {item['id']} [{item['status']}] {item['actor']} ({item['client_name']}/{item['model_name']})" for item in active_sessions] or ["- None"])
        markdown = "\n".join(lines).rstrip() + "\n"
        context_path = pcfg.context_path / "RESUME_PACKET.md"
        if write_files:
            write_text_atomic(context_path, markdown)
            if session_id:
                write_text_atomic(pcfg.sessions_path / session_id / "resume_packet.md", markdown)
        self._record_token_usage_metric(
            operation="generate_resume_packet",
            project_path=inferred_path,
            estimated_output_tokens=self._estimated_tokens(markdown),
            compact_output_tokens=self._estimated_tokens(markdown),
            compact_chars=len(markdown),
            metadata={
                "session_id": session_id,
                "task_id": (task or {}).get("id"),
                "write_files": write_files,
            },
        )
        return {"project_slug": pcfg.project_slug, "project_path": str(pcfg.project_path), "path": str(context_path), "markdown": markdown}

    def generate_emergency_handoff(
        self,
        session_id: str | None = None,
        task_id: str | None = None,
        project_path: str | None = None,
    ) -> dict[str, Any]:
        inferred_path = project_path or self._find_project_path_for_session(session_id) or self._find_project_path_for_task(task_id)
        pcfg = self._project_config_for(inferred_path)
        store = self._store(inferred_path)
        session = store.get_session(session_id) if session_id else None
        task = store.get_task(task_id) if task_id else store.get_current_task()
        recent_work = store.get_recent_work(limit=6)
        blockers = store.get_blockers(open_only=True, limit=5)
        relevant_files = store.get_relevant_files(task_id=task["id"] if task else task_id)
        summary = recent_work[0]["message"] if recent_work else "Session ended before a clean handoff could be written."
        next_steps = "Resume from the current task, review the recent work and relevant files, then continue implementation."
        open_questions = "; ".join(item["title"] for item in blockers[:3]) or "No explicit blockers recorded."
        note = f"Recovered from {'session ' + session_id if session_id else 'an interrupted session'} in project {pcfg.project_slug}."
        markdown = "\n".join(
            [
                "# Emergency Handoff",
                "",
                f"- Project: {pcfg.project_name}",
                f"- Session: {session_id or 'unknown'}",
                f"- Task: {(task or {}).get('id', 'none')}",
                "",
                "## Summary",
                "",
                summary,
                "",
                "## Next Steps",
                "",
                next_steps,
                "",
                "## Open Questions",
                "",
                open_questions,
                "",
                "## Relevant Files",
                "",
                *([f"- {item}" for item in relevant_files] or ["- None"]),
                "",
                "## Recovery Note",
                "",
                note,
                "",
            ]
        )
        path = pcfg.context_path / "HANDOFF_EMERGENCY.md"
        write_text_atomic(path, markdown)
        if session_id:
            write_text_atomic(pcfg.sessions_path / session_id / "handoff_emergency.md", markdown)
        return {
            "project_slug": pcfg.project_slug,
            "project_path": str(pcfg.project_path),
            "session_id": session_id,
            "task_id": (task or {}).get("id"),
            "summary": summary,
            "next_steps": next_steps,
            "open_questions": open_questions,
            "relevant_files": relevant_files,
            "note": note,
            "path": str(path),
            "markdown": markdown,
        }

    def recover_session(
        self,
        session_id: str | None = None,
        actor: str = "recovery",
        project_path: str | None = None,
    ) -> dict[str, Any]:
        inferred_path = project_path or self._find_project_path_for_session(session_id)
        store = self._store(inferred_path)
        if not session_id:
            sessions = store.get_active_sessions(limit=1)
            if not sessions:
                raise ValueError("No open sessions available to recover.")
            session_id = sessions[0]["id"]
        emergency = self.generate_emergency_handoff(session_id=session_id, project_path=inferred_path)
        handoff = self.create_handoff(
            summary=emergency["summary"],
            next_steps=emergency["next_steps"],
            open_questions=emergency["open_questions"],
            note=emergency["note"],
            task_id=emergency["task_id"],
            from_actor=actor,
            to_actor="next-agent",
            project_path=inferred_path,
        )
        resume = self.generate_resume_packet(session_id=session_id, project_path=inferred_path)
        return {"recovered": True, "session_id": session_id, "handoff": handoff, "resume_packet": resume, "emergency_handoff": emergency}

    def generate_cross_tool_handoff(
        self,
        handoff_id: int | None = None,
        session_id: str | None = None,
        target_tool: str = "claude-code",
        target_env: str = "default",
        project_path: str | None = None,
    ) -> dict[str, Any]:
        """Generate a structured cross-tool handoff for a specific target environment.

        Produces a compact JSON payload with all context needed for another tool or
        agent to resume work — including task state, relevant files, recent decisions,
        blockers, and session lineage chain.
        """
        store = self._store(project_path)
        task = None
        relevant_files: list[str] = []
        decisions: list[dict[str, Any]] = []
        blockers: list[dict[str, Any]] = []
        recent_work: list[dict[str, Any]] = []
        lineage_chain: list[dict[str, Any]] = []

        if session_id:
            lineage = store.get_session_lineage_chain(session_id)
            lineage_chain = lineage

        if handoff_id is not None:
            handoff = store.get_handoff(handoff_id)
            if handoff and handoff.get("task_id"):
                task = store.get_task(handoff["task_id"])
                relevant_files = store.get_relevant_files(handoff["task_id"])
                decisions = store.get_decisions(limit=8)
                blockers = store.get_blockers(open_only=True, limit=5)
                recent_work = store.get_recent_work(limit=6)
        elif session_id:
            session = store.get_session(session_id)
            if session and session.get("task_id"):
                task = store.get_task(session["task_id"])
                relevant_files = store.get_relevant_files(session["task_id"])
            decisions = store.get_decisions(limit=8)
            blockers = store.get_blockers(open_only=True, limit=5)
            recent_work = store.get_recent_work(limit=6)
        else:
            task_dict = store.get_current_task()
            if task_dict:
                task = task_dict
                relevant_files = store.get_relevant_files(task_dict["id"])
            decisions = store.get_decisions(limit=8)
            blockers = store.get_blockers(open_only=True, limit=5)
            recent_work = store.get_relevant_files(task_dict["id"] if task_dict else None) if task_dict else []

        env_info = store.get_session_env_info(session_id) if session_id else None
        structured_payload = {
            "handoff_version": "1.0",
            "target_tool": target_tool,
            "target_env": target_env,
            "generated_at": utc_now(),
            "task": {
                "id": (task or {}).get("id"),
                "title": (task or {}).get("title"),
                "status": (task or {}).get("status"),
                "priority": (task or {}).get("priority"),
                "description": (task or {}).get("description"),
            } if task else None,
            "relevant_files": relevant_files[:12],
            "recent_decisions": [{"id": d["id"], "title": d["title"], "decision": d["decision"][:200]} for d in decisions],
            "open_blockers": [{"id": b["id"], "title": b["title"]} for b in blockers],
            "recent_work": [{"message": w["message"], "actor": w["actor"], "created_at": w["created_at"]} for w in recent_work],
            "session_lineage": [
                {"session_id": s["session_id"], "actor": s["actor"], "depth": s["lineage_depth"]}
                for s in lineage_chain
            ],
            "ide_env": {
                "ide_name": (env_info or {}).get("ide_name"),
                "ide_version": (env_info or {}).get("ide_version"),
                "ide_platform": (env_info or {}).get("ide_platform"),
                "os_name": (env_info or {}).get("os_name"),
                "os_version": (env_info or {}).get("os_version"),
            } if env_info else None,
        }

        handoff_record = store.get_latest_handoff() if handoff_id is None else None
        effective_handoff_id = handoff_id or (handoff_record["id"] if handoff_record else 0)

        if effective_handoff_id > 0:
            cross = store.create_cross_tool_handoff(
                handoff_id=effective_handoff_id,
                target_tool=target_tool,
                target_env=target_env,
                structured_payload=structured_payload,
            )
            self.sync_all(project_path)
            return {"cross_tool_handoff": cross, "structured_payload": structured_payload}

        return {"structured_payload": structured_payload, "cross_tool_handoff": None}

    def get_session_lineage_chain(
        self,
        session_id: str,
        max_depth: int = 10,
        project_path: str | None = None,
    ) -> list[dict[str, Any]]:
        """Get the full lineage chain from session_id to root ancestor."""
        return self._store(project_path).get_session_lineage_chain(session_id, max_depth=max_depth)

    def set_session_environment(
        self,
        session_id: str,
        ide_name: str = "",
        ide_version: str = "",
        ide_platform: str = "",
        os_name: str = "",
        os_version: str = "",
        env_variables: dict[str, str] | None = None,
        startup_context: dict[str, Any] | None = None,
        parent_session_id: str | None = None,
        project_path: str | None = None,
    ) -> dict[str, Any]:
        """Attach IDE/environment metadata to a session and optionally set its parent lineage."""
        store = self._store(project_path)
        result = store.upsert_session_env_info(
            session_id=session_id,
            ide_name=ide_name,
            ide_version=ide_version,
            ide_platform=ide_platform,
            os_name=os_name,
            os_version=os_version,
            env_variables=env_variables,
            startup_context=startup_context,
        )
        if parent_session_id:
            parent_lineage = store.get_session_lineage(parent_session_id)
            depth = (parent_lineage.get("lineage_depth", 0) + 1) if parent_lineage else 1
            store.upsert_session_lineage(
                session_id=session_id,
                parent_session_id=parent_session_id,
                lineage_depth=depth,
            )
        self.sync_all(project_path)
        return result or {}

    def _detect_project_type(self, project_path: Path) -> list[str]:
        """Detect project types from file patterns in the root directory."""
        types: list[str] = []
        root_files = set(f.name for f in project_path.iterdir() if f.is_file())
        root_names = set(p.name for p in project_path.iterdir())

        if "pyproject.toml" in root_files or "setup.py" in root_files or "setup.cfg" in root_files:
            types.append("python")
        if any(f.name == "package.json" for f in project_path.iterdir() if f.is_file()):
            types.append("javascript")
        if any(f.suffix == ".ts" and f.name == "package.json" for f in project_path.iterdir() if f.is_file()):
            types.append("typescript")
        if "Cargo.toml" in root_files:
            types.append("rust")
        if "go.mod" in root_files:
            types.append("go")
        if "pom.xml" in root_files or "build.gradle" in root_files:
            types.append("java")
        if ".NETFramework" in root_names or any(f.name.endswith(".csproj") for f in project_path.iterdir() if f.is_file()):
            types.append("csharp")
        if "Makefile" in root_files and not types:
            types.append("c-cpp")
        if any(f.suffix in {".c", ".h"} for f in project_path.iterdir() if f.is_file()):
            types.append("c-cpp")
        if "requirements.txt" in root_files:
            types.append("python")
        if "package-lock.json" in root_files or "yarn.lock" in root_files:
            types.append("javascript")
        if "Cargo.lock" in root_files:
            types.append("rust")
        if ".gitmodules" in root_files:
            types.append("git-submodule")
        if ".git" in root_names:
            types.append("git")
        return types

    def _detect_workspace_type(self, project_path: Path) -> str:
        """Detect workspace/mono-repo type."""
        root_names = [p.name for p in project_path.iterdir() if p.is_dir()]
        if "packages" in root_names or "apps" in root_names or "services" in root_names:
            return "mono-repo"
        if "src" in root_names and "tests" in root_names:
            return "standard"
        if "libs" in root_names or "shared" in root_names:
            return "library-mono"
        return "single-repo"

    def _scan_nearby_projects(self, project_path: Path, max_distance: int = 3) -> list[dict[str, Any]]:
        """Scan nearby directories for other projects at shallow depth."""
        candidates: list[dict[str, Any]] = []
        parent = project_path.parent.resolve()
        try:
            entries = list(parent.iterdir())
        except OSError:
            return candidates
        for entry in entries[:50]:
            if not entry.is_dir():
                continue
            if entry == project_path.resolve():
                continue
            depth = len(entry.parts) - len(parent.parts)
            if depth > max_distance:
                continue
            bridge = entry / ".obsmcp-link.json"
            if bridge.exists():
                continue
            git_root = entry / ".git"
            if git_root.exists():
                try:
                    rel = entry.relative_to(parent)
                except ValueError:
                    continue
                project_types = self._detect_project_type(entry)
                workspace_type = self._detect_workspace_type(entry)
                candidates.append({
                    "path": str(entry),
                    "relative_path": str(rel),
                    "depth": depth,
                    "project_types": project_types,
                    "workspace_type": workspace_type,
                })
        return sorted(candidates, key=lambda c: c["depth"])

    def _build_project_resolution_payload(
        self,
        inferred_path: str,
        *,
        already_registered: bool,
        scan_nearby: bool = False,
        resolution_source: str | None = None,
        matched_hint: str | None = None,
        ide_name: str | None = None,
    ) -> dict[str, Any]:
        pcfg = self._project_config_for(inferred_path)
        nearby = self._scan_nearby_projects(pcfg.project_path) if scan_nearby else []
        payload = {
            "resolved": True,
            "already_registered": already_registered,
            "project_slug": pcfg.project_slug,
            "project_path": str(pcfg.project_path),
            "workspace_root": str(pcfg.workspace_root),
            "project_types": self._detect_project_type(pcfg.project_path),
            "workspace_type": self._detect_workspace_type(pcfg.project_path),
            "nearby_projects": nearby[:5],
        }
        if resolution_source:
            payload["resolution_source"] = resolution_source
        if matched_hint:
            payload["matched_hint"] = matched_hint
        if ide_name:
            payload["ide_name"] = ide_name
        return payload

    def _resolve_project_from_ide_metadata(
        self,
        *,
        project_path: str | None = None,
        project_slug: str | None = None,
        session_id: str | None = None,
        task_id: str | None = None,
        repo_path: str | None = None,
        cwd: str | None = None,
        workspace_path: str | None = None,
        workspace_folders: list[str] | None = None,
        active_file: str | None = None,
        open_files: list[str] | None = None,
        env_variables: dict[str, str] | None = None,
    ) -> tuple[str | None, str | None, str | None]:
        if project_slug:
            record = self.registry.get_by_slug(project_slug)
            if record:
                return record["repo_path"], "project_slug", project_slug
        if project_path:
            return project_path, "project_path", project_path
        if session_id:
            inferred = self._find_project_path_for_session(session_id)
            if inferred:
                return inferred, "session_id", session_id
        if task_id:
            inferred = self._find_project_path_for_task(task_id)
            if inferred:
                return inferred, "task_id", task_id

        env_variables = env_variables or {}
        path_candidates: list[tuple[str, str]] = []

        for label, value in [
            ("repo_path", repo_path),
            ("workspace_path", workspace_path),
            ("cwd", cwd),
        ]:
            if isinstance(value, str) and value.strip():
                path_candidates.append((label, value))

        for idx, value in enumerate(workspace_folders or []):
            if isinstance(value, str) and value.strip():
                path_candidates.append((f"workspace_folders[{idx}]", value))

        if isinstance(active_file, str) and active_file.strip():
            path_candidates.append(("active_file", active_file))

        for idx, value in enumerate(open_files or []):
            if isinstance(value, str) and value.strip():
                path_candidates.append((f"open_files[{idx}]", value))

        for key in (
            "OBSMCP_PROJECT",
            "PROJECT_PATH",
            "PROJECT_ROOT",
            "REPO_PATH",
            "REPO_ROOT",
            "WORKSPACE_PATH",
            "WORKSPACE_ROOT",
            "WORKSPACE_FOLDER",
            "PWD",
            "INIT_CWD",
        ):
            value = env_variables.get(key)
            if isinstance(value, str) and value.strip():
                path_candidates.append((f"env:{key}", value))

        for source, hint in path_candidates:
            inferred = self._registered_project_for_path_hint(hint)
            if inferred:
                return inferred, source, hint

        return None, None, None

    def get_or_create_project(
        self,
        project_path: str | None = None,
        session_id: str | None = None,
        task_id: str | None = None,
        auto_register: bool = True,
        project_name: str | None = None,
        tags: list[str] | None = None,
        scan_nearby: bool = False,
    ) -> dict[str, Any]:
        """Auto-detect or create a project from a path hint, session, task, or environment.

        This method resolves a project path from multiple sources (explicit, session,
        task, env, cwd, file paths) and optionally registers it if not already known.
        It also returns nearby detected projects and project type metadata.
        """
        inferred = self._infer_project_path({"session_id": session_id, "task_id": task_id, "path": project_path})
        if inferred:
            provisional = self.config.get_project_config(inferred)
            if self.registry.get_by_slug(provisional.project_slug):
                return self._build_project_resolution_payload(inferred, already_registered=True, scan_nearby=scan_nearby)

        if not auto_register:
            return {"resolved": False, "project_path": None}

        if inferred:
            registered = self.register_project(repo_path=inferred, name=project_name, tags=tags)
            payload = self._build_project_resolution_payload(inferred, already_registered=False, scan_nearby=scan_nearby)
            payload["project_slug"] = registered.get("slug", payload["project_slug"])
            return payload

        return {"resolved": False, "project_path": None}

    def resolve_active_project(
        self,
        project_path: str | None = None,
        project_slug: str | None = None,
        session_id: str | None = None,
        task_id: str | None = None,
        repo_path: str | None = None,
        cwd: str | None = None,
        workspace_path: str | None = None,
        workspace_folders: list[str] | None = None,
        active_file: str | None = None,
        open_files: list[str] | None = None,
        env_variables: dict[str, str] | None = None,
        auto_register: bool = True,
        project_name: str | None = None,
        tags: list[str] | None = None,
        scan_nearby: bool = False,
        ide_name: str = "",
    ) -> dict[str, Any]:
        """Resolve the active project from IDE metadata before the first continuity write."""
        inferred, source, matched_hint = self._resolve_project_from_ide_metadata(
            project_path=project_path,
            project_slug=project_slug,
            session_id=session_id,
            task_id=task_id,
            repo_path=repo_path,
            cwd=cwd,
            workspace_path=workspace_path,
            workspace_folders=workspace_folders,
            active_file=active_file,
            open_files=open_files,
            env_variables=env_variables,
        )
        if not inferred:
            return {
                "resolved": False,
                "already_registered": False,
                "project_slug": None,
                "project_path": None,
                "workspace_root": None,
                "resolution_source": None,
                "matched_hint": None,
                "ide_name": ide_name,
                "requires_registration": False,
                "reason": "No project hint could be resolved from IDE metadata.",
                "recommended_action": "Pass project_path, cwd, active_file, workspace_folders, repo_path, session_id, or task_id.",
            }

        provisional = self.config.get_project_config(inferred)
        registered = self.registry.get_by_slug(provisional.project_slug)
        if registered:
            return self._build_project_resolution_payload(
                inferred,
                already_registered=True,
                scan_nearby=scan_nearby,
                resolution_source=source,
                matched_hint=matched_hint,
                ide_name=ide_name,
            )

        if not auto_register:
            return {
                "resolved": False,
                "already_registered": False,
                "project_slug": provisional.project_slug,
                "project_path": str(provisional.project_path),
                "workspace_root": str(provisional.workspace_root),
                "resolution_source": source,
                "matched_hint": matched_hint,
                "ide_name": ide_name,
                "requires_registration": True,
                "reason": "Project was inferred from IDE metadata but is not registered yet.",
                "recommended_action": "Retry with auto_register=true or call register_project with the inferred project_path.",
            }

        registered_record = self.register_project(repo_path=inferred, name=project_name, tags=tags)
        payload = self._build_project_resolution_payload(
            inferred,
            already_registered=False,
            scan_nearby=scan_nearby,
            resolution_source=source,
            matched_hint=matched_hint,
            ide_name=ide_name,
        )
        payload["project_slug"] = registered_record.get("slug", payload["project_slug"])
        return payload

    def generate_startup_prompt_template(self, first_contact: bool = True, project_path: str | None = None) -> str:
        prompt_path = self.config.root_dir / "master prompt.md"
        policy = self._resolve_output_policy(
            operation_kind="general",
            project_path=project_path,
        )
        if prompt_path.exists():
            base = prompt_path.read_text(encoding="utf-8").rstrip()
            appendix = [
                "",
                "## Startup Hints",
                "",
                "- Prefer `generate_startup_context()` or `get_fast_path_response(kind=\"startup_context\")` for low-latency startup.",
                "- Prefer `get_recent_commands`, `get_last_command_result`, and `get_command_failures` over replaying raw terminal output.",
                "- Use `get_command_execution_policy` before batching or reviewing terminal commands.",
            ]
            if policy.prompt_contract:
                appendix.extend(["", policy.prompt_contract.strip()])
            rendered = base + "\n" + "\n".join(appendix) + "\n"
            self._record_output_policy_metric(
                operation="generate_startup_prompt_template",
                policy=policy,
                rendered_text=rendered,
                project_path=project_path,
            )
            return rendered

        lines = [
            "# Master Prompt",
            "",
            "Use obsmcp as the primary continuity system.",
        ]
        if first_contact:
            lines.append("This is a first-contact startup prompt.")
        lines.extend(
            [
                "",
                "Startup hints:",
                "- Prefer generate_startup_context for startup/resume.",
                "- Prefer recent command summaries over raw terminal replay.",
            ]
        )
        if policy.prompt_contract:
            lines.extend(["", policy.prompt_contract.strip()])
        lines.append("")
        rendered = "\n".join(lines)
        self._record_output_policy_metric(
            operation="generate_startup_prompt_template",
            policy=policy,
            rendered_text=rendered,
            project_path=project_path,
        )
        return rendered

    # ---------------------------------------------------------------------------
    # Code Atlas
    # ---------------------------------------------------------------------------

    def scan_codebase(
        self,
        project_path: str | None = None,
        force_refresh: bool = False,
        background: bool = False,
        requested_by: str = "unknown",
    ) -> dict[str, Any]:
        """Scan the entire project and generate (or refresh) the Code Atlas.

        Args:
            project_path: Project root to scan (defaults to the active project).
            force_refresh: If True, always regenerate. If False, only regenerate
                           if the atlas is missing or older than any source file.
            background: If True, queue the scan and return a pollable job instead of blocking.
        """
        with span("code_atlas.scan", project_path=project_path, force_refresh=force_refresh, background=background):
            if not self._atlas_needs_refresh(project_path, force_refresh=force_refresh):
                return self._current_atlas_metadata(project_path)
            if not background:
                return self._scan_codebase_sync(project_path=project_path, force_refresh=force_refresh)
            job = self.start_scan_job(project_path=project_path, force_refresh=force_refresh, requested_by=requested_by)
            job["scan_required"] = True
            job["current_atlas"] = self._current_atlas_metadata(project_path) if (self._project_config_for(project_path).vault_path / "Research" / "Code Atlas.md").exists() else None
            return job

    def get_code_atlas_status(self, project_path: str | None = None) -> dict[str, Any]:
        """Return current atlas status without regenerating."""
        pcfg = self._project_config_for(project_path)
        atlas_path = pcfg.vault_path / "Research" / "Code Atlas.md"
        active_job = self._store(project_path).get_active_scan_job(job_type="code_atlas")
        if not atlas_path.exists():
            return {
                "exists": False,
                "message": "Code Atlas has not been generated yet.",
                "hint": "Call scan_codebase() to generate it.",
                "active_job": active_job,
            }

        cached = read_json_with_retry(pcfg.json_export_dir / "code_atlas.json", {})
        if cached:
            semantic_stats = self._store(project_path).get_symbol_index_stats()
            mtime = datetime.fromtimestamp(atlas_path.stat().st_mtime, tz=timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
            return {
                "exists": True,
                "path": str(atlas_path),
                "last_generated": mtime,
                "total_files": cached.get("total_files", 0),
                "total_lines": cached.get("total_lines", 0),
                "languages": cached.get("languages", {}),
                "semantic_index": semantic_stats,
                "hint": "Call scan_codebase(force_refresh=True) to regenerate.",
                "active_job": active_job,
            }
        atlas = self._build_code_atlas(project_path)
        result = atlas.scan()
        _, _, _, semantic_stats = self._refresh_semantic_index(project_path=str(pcfg.project_path), atlas_result=result)
        mtime = datetime.fromtimestamp(atlas_path.stat().st_mtime, tz=timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")

        return {
            "exists": True,
            "path": str(atlas_path),
            "last_generated": mtime,
            "total_files": result.total_files,
            "total_lines": result.total_lines,
            "languages": result.languages,
            "semantic_index": semantic_stats,
            "hint": "Call scan_codebase(force_refresh=True) to regenerate.",
            "active_job": active_job,
        }

    # ---------------------------------------------------------------------------
    # Semantic Knowledge
    # ---------------------------------------------------------------------------

    def describe_module(self, module_path: str, project_path: str | None = None, force_llm: bool = False) -> dict[str, Any]:
        pcfg, _, index, _ = self._refresh_semantic_index(project_path)
        entity = index.get_module(module_path)
        if not entity:
            raise ValueError(f"Module '{module_path}' was not found in project {pcfg.project_slug}.")
        return self._describe_entity(entity.to_index_row(), index, project_path=str(pcfg.project_path), force_llm=force_llm)

    def describe_symbol(
        self,
        symbol_name: str | None = None,
        module_path: str | None = None,
        entity_key: str | None = None,
        entity_type: str | None = None,
        project_path: str | None = None,
        force_llm: bool = False,
    ) -> dict[str, Any]:
        pcfg, _, index, _ = self._refresh_semantic_index(project_path)
        if entity_key:
            entity = index.entity_map.get(entity_key)
            if not entity:
                raise ValueError(f"Entity '{entity_key}' was not found in project {pcfg.project_slug}.")
            return self._describe_entity(entity.to_index_row(), index, project_path=str(pcfg.project_path), force_llm=force_llm)

        if not symbol_name:
            raise ValueError("describe_symbol requires symbol_name or entity_key.")
        allowed_types = [entity_type] if entity_type in {"function", "class"} else None
        candidates = index.get_symbol_candidates(symbol_name, module_path=module_path, entity_types=allowed_types)
        if not candidates:
            return {
                "status": "not_found",
                "message": f"No symbol named '{symbol_name}' was found.",
                "query": {"symbol_name": symbol_name, "module_path": module_path, "entity_type": entity_type},
            }
        if len(candidates) > 1:
            return {
                "status": "ambiguous",
                "message": f"Multiple symbols named '{symbol_name}' were found.",
                "candidates": [item.to_index_row() for item in candidates],
            }
        return self._describe_entity(candidates[0].to_index_row(), index, project_path=str(pcfg.project_path), force_llm=force_llm)

    def describe_feature(self, feature_name: str, project_path: str | None = None, force_llm: bool = False) -> dict[str, Any]:
        pcfg, _, index, _ = self._refresh_semantic_index(project_path)
        entity = index.get_feature(feature_name)
        if not entity:
            normalized = feature_name.lower()
            module_matches = [
                item
                for item in index.entities
                if item.entity_type == "module"
                and (
                    normalized in [tag.lower() for tag in item.feature_tags]
                    or item.metadata.get("language", "").lower() == normalized
                )
            ]
            if not module_matches:
                return {"status": "not_found", "message": f"No feature named '{feature_name}' was found."}
            source_files = sorted({item.file_path for item in module_matches})
            entity = type(module_matches[0])(
                entity_key=f"feature:{feature_name.lower()}",
                entity_type="feature",
                name=feature_name,
                file_path=source_files[0],
                symbol_path=feature_name,
                signature="",
                line_number=1,
                feature_tags=[feature_name],
                source_files=source_files,
                source_fingerprint="|".join(item.source_fingerprint for item in module_matches),
                summary_hint=f"Feature `{feature_name}` inferred from {len(source_files)} module(s).",
                metadata={"files": source_files, "language_count": len({item.metadata.get('language', '') for item in module_matches})},
            )
            index.entity_map[entity.entity_key] = entity
        return self._describe_entity(entity.to_index_row(), index, project_path=str(pcfg.project_path), force_llm=force_llm)

    def search_code_knowledge(self, query: str, limit: int = 10, project_path: str | None = None) -> dict[str, Any]:
        pcfg, _, index, _ = self._refresh_semantic_index(project_path)
        matches = index.search(query, limit=limit)
        store = self._store(project_path)
        results = []
        for entity in matches:
            cached = store.get_semantic_description(entity.entity_key)
            results.append(
                {
                    "entity_key": entity.entity_key,
                    "entity_type": entity.entity_type,
                    "name": entity.name,
                    "file_path": entity.file_path,
                    "symbol_path": entity.symbol_path,
                    "summary_hint": (cached or {}).get("purpose") or entity.summary_hint,
                    "freshness": (cached or {}).get("freshness", "unverified"),
                    "feature_tags": entity.feature_tags,
                }
            )
        return {"query": query, "project_slug": pcfg.project_slug, "match_count": len(results), "results": results}

    def get_symbol_candidates(
        self,
        symbol_name: str,
        module_path: str | None = None,
        entity_type: str | None = None,
        limit: int = 20,
        project_path: str | None = None,
    ) -> dict[str, Any]:
        pcfg, _, index, _ = self._refresh_semantic_index(project_path)
        allowed_types = [entity_type] if entity_type in {"function", "class"} else None
        candidates = index.get_symbol_candidates(symbol_name, module_path=module_path, entity_types=allowed_types)[:limit]
        return {"symbol_name": symbol_name, "project_slug": pcfg.project_slug, "candidates": [item.to_index_row() for item in candidates]}

    def get_related_symbols(self, entity_key: str, limit: int = 8, project_path: str | None = None) -> dict[str, Any]:
        pcfg, _, index, _ = self._refresh_semantic_index(project_path)
        entity = index.entity_map.get(entity_key)
        if not entity:
            raise ValueError(f"Entity '{entity_key}' was not found in project {pcfg.project_slug}.")
        return {"entity_key": entity_key, "related_symbols": [item.to_index_row() for item in index.related_symbols(entity, limit=limit)]}

    def invalidate_semantic_cache(
        self,
        entity_key: str | None = None,
        file_paths: list[str] | None = None,
        project_path: str | None = None,
    ) -> dict[str, Any]:
        result = self._store(project_path).invalidate_semantic_cache(entity_key=entity_key, file_paths=file_paths)
        self.sync_all(project_path)
        return result

    def refresh_semantic_description(
        self,
        entity_key: str | None = None,
        module_path: str | None = None,
        symbol_name: str | None = None,
        feature_name: str | None = None,
        entity_type: str | None = None,
        force_llm: bool = False,
        project_path: str | None = None,
    ) -> dict[str, Any]:
        if entity_key:
            self.invalidate_semantic_cache(entity_key=entity_key, project_path=project_path)
            return self.describe_symbol(entity_key=entity_key, project_path=project_path, force_llm=force_llm)
        if module_path and not symbol_name and not feature_name:
            self.invalidate_semantic_cache(file_paths=[module_path], project_path=project_path)
            pcfg, _, index, _ = self._refresh_semantic_index(project_path)
            entity = index.get_module(module_path)
            if not entity:
                raise ValueError(f"Module '{module_path}' was not found in project {pcfg.project_slug}.")
            return self._describe_entity(
                entity.to_index_row(),
                index,
                project_path=str(pcfg.project_path),
                force_llm=force_llm,
                force_refresh=True,
            )
        if symbol_name:
            return self.describe_symbol(symbol_name=symbol_name, module_path=module_path, entity_type=entity_type, project_path=project_path, force_llm=force_llm)
        if feature_name:
            return self.describe_feature(feature_name=feature_name, project_path=project_path, force_llm=force_llm)
        raise ValueError("refresh_semantic_description requires entity_key, module_path, symbol_name, or feature_name.")

    # =========================================================================
    # Phase 1: Task Templates (service layer)
    # =========================================================================

    def create_task_from_template(
        self,
        template_name: str,
        variables: dict[str, str] | None = None,
        actor: str = "unknown",
        session_id: str | None = None,
        project_path: str | None = None,
    ) -> dict[str, Any]:
        result = self._store(project_path).create_task_from_template(
            template_name=template_name,
            variables=variables,
            actor=actor,
            session_id=session_id,
        )
        self.sync_all(project_path)
        return result

    def get_task_templates(self, project_path: str | None = None) -> list[dict[str, Any]]:
        return self._store(project_path).get_task_templates()

    def get_task_template(self, name: str, project_path: str | None = None) -> dict[str, Any] | None:
        return self._store(project_path).get_task_template(name)

    def create_task_template(
        self,
        name: str,
        title_template: str,
        description_template: str,
        priority: str = "medium",
        tags: list[str] | None = None,
        project_path: str | None = None,
    ) -> dict[str, Any]:
        result = self._store(project_path).create_task_template(
            name=name,
            title_template=title_template,
            description_template=description_template,
            priority=priority,
            tags=tags,
        )
        self.sync_all(project_path)
        return result

    def delete_task_template(self, name: str, project_path: str | None = None) -> dict[str, Any]:
        result = self._store(project_path).delete_task_template(name)
        self.sync_all(project_path)
        return result

    # =========================================================================
    # Phase 1: Quick Log
    # =========================================================================

    def quick_log(
        self,
        message: str,
        files: list[str] | None = None,
        actor: str = "quick-log",
        session_id: str | None = None,
        project_path: str | None = None,
    ) -> dict[str, Any]:
        result = self._store(project_path).quick_log(message=message, files=files, actor=actor, session_id=session_id)
        self.sync_all(project_path)
        return result

    # =========================================================================
    # Phase 1: Audit Log
    # =========================================================================

    def get_audit_log(
        self,
        actor: str | None = None,
        task_id: str | None = None,
        action_type: str | None = None,
        from_date: str | None = None,
        to_date: str | None = None,
        limit: int = 100,
        include_ai_only: bool = False,
        project_path: str | None = None,
    ) -> dict[str, Any]:
        return self._store(project_path).get_audit_log(
            actor=actor,
            task_id=task_id,
            action_type=action_type,
            from_date=from_date,
            to_date=to_date,
            limit=limit,
            include_ai_only=include_ai_only,
        )

    # =========================================================================
    # Phase 2: Reset Project
    # =========================================================================

    def reset_project(self, scope: str, actor: str = "unknown", project_path: str | None = None) -> dict[str, Any]:
        result = self._store(project_path).reset_project(scope=scope, actor=actor)
        self.sync_all(project_path)
        result["post_reset_snapshot"] = self.get_project_status_snapshot(project_path=project_path)
        return result

    # =========================================================================
    # Phase 2: Bulk Task Operations
    # =========================================================================

    def bulk_task_ops(self, operations: list[dict[str, Any]], actor: str = "unknown", project_path: str | None = None) -> dict[str, Any]:
        result = self._store(project_path).bulk_task_ops(operations=operations, actor=actor)
        if result["failed"] == 0:
            self.sync_all(project_path)
        return result

    # =========================================================================
    # Phase 2: Project Export
    # =========================================================================

    def export_project(self, format: str = "json", project_path: str | None = None) -> dict[str, Any]:
        return self._store(project_path).export_project(format=format)

    # =========================================================================
    # Phase 3: Work Log Expiry
    # =========================================================================

    def configure_log_expiry(self, days: int, actor: str = "unknown", project_path: str | None = None) -> dict[str, Any]:
        return self._store(project_path).configure_log_expiry(days=days, actor=actor)

    def expire_old_logs(self, actor: str = "unknown", project_path: str | None = None) -> dict[str, Any]:
        result = self._store(project_path).expire_old_logs(actor=actor)
        self.sync_all(project_path)
        return result

    def get_log_stats(self, project_path: str | None = None) -> dict[str, Any]:
        return self._store(project_path).get_log_stats()

    # =========================================================================
    # Phase 3: Session Replay
    # =========================================================================

    def session_replay(self, session_id: str | None = None, project_path: str | None = None) -> dict[str, Any]:
        return self._store(project_path).session_replay(session_id=session_id)

    # =========================================================================
    # Phase 3: Task Dependencies
    # =========================================================================

    def add_task_dependency(self, task_id: str, blocked_by: list[str] | None = None, blocks: list[str] | None = None, project_path: str | None = None) -> dict[str, Any]:
        result = self._store(project_path).add_task_dependency(task_id=task_id, blocked_by=blocked_by, blocks=blocks)
        self.sync_all(project_path)
        return result

    def remove_task_dependency(self, task_id: str, blocked_by: list[str] | None = None, blocks: list[str] | None = None, project_path: str | None = None) -> dict[str, Any]:
        result = self._store(project_path).remove_task_dependency(task_id=task_id, blocked_by=blocked_by, blocks=blocks)
        self.sync_all(project_path)
        return result

    def get_task_dependency(self, task_id: str, project_path: str | None = None) -> dict[str, Any] | None:
        return self._store(project_path).get_task_dependency(task_id=task_id)

    def get_all_dependencies(self, project_path: str | None = None) -> list[dict[str, Any]]:
        return self._store(project_path).get_all_dependencies()

    def get_blocked_tasks(self, project_path: str | None = None) -> list[dict[str, Any]]:
        return self._store(project_path).get_blocked_tasks()

    def validate_dependencies(self, project_path: str | None = None) -> dict[str, Any]:
        return self._store(project_path).validate_dependencies()

    def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> Any:
        log = get_logger("obsmcp")
        log.info("tool_started", tool_name=name, has_args=bool(arguments))
        arguments = dict(arguments) if arguments else {}  # copy to avoid mutating
        inferred_project_path = self._infer_project_path(arguments)
        if inferred_project_path:
            arguments["project_path"] = inferred_project_path
        elif self.config.strict_project_routing and self._tool_requires_project_context(name):
            raise self._missing_project_context_error(name)
        with span("tool.dispatch", tool_name=name, project_path=inferred_project_path):
            if name == "scan_codebase" and "background" not in arguments:
                arguments["background"] = True
            if name not in {"resolve_project", "get_project_workspace_paths"}:
                arguments.pop("project_slug", None)
        handlers: dict[str, Callable[..., Any]] = {
            "register_project": self.register_project,
            "list_projects": self.list_projects,
            "resolve_project": self.resolve_project,
            "resolve_active_project": self.resolve_active_project,
            "get_project_workspace_paths": self.get_project_workspace_paths,
            "attach_repo_bridge": self.attach_repo_bridge,
            "migrate_project_layout": self.migrate_project_layout,
            "get_project_brief": self.get_project_brief,
            "get_current_task": self.get_current_task,
            "get_active_tasks": self.get_active_tasks,
            "get_latest_handoff": self.get_latest_handoff,
            "get_recent_work": self.get_recent_work,
            "get_decisions": self.get_decisions,
            "get_blockers": self.get_blockers,
            "get_relevant_files": self.get_relevant_files,
            "get_table_schema": self.get_table_schema,
            "search_notes": self.search_notes,
            "read_note": self.read_note,
            "get_project_status_snapshot": self.get_project_status_snapshot,
            "log_work": self.log_work,
            "log_checkpoint": self.log_checkpoint,
            "update_task": self.update_task,
            "create_task": self.create_task,
            "get_task_progress": self.get_task_progress,
            "log_decision": self.log_decision,
            "log_blocker": self.log_blocker,
            "resolve_blocker": self.resolve_blocker,
            "create_handoff": self.create_handoff,
            "append_handoff_note": self.append_handoff_note,
            "update_project_brief_section": self.update_project_brief_section,
            "create_daily_note_entry": self.create_daily_note_entry,
            "sync_context_files": self.sync_context,
            "session_open": self.session_open,
            "session_heartbeat": self.session_heartbeat,
            "session_close": self.session_close,
            "get_active_sessions": self.get_active_sessions,
            "detect_missing_writeback": self.detect_missing_writeback,
            "get_startup_preflight": self.get_startup_preflight,
            "get_resume_board": self.get_resume_board,
            "generate_resume_packet": self.generate_resume_packet,
            "generate_emergency_handoff": self.generate_emergency_handoff,
            "recover_session": self.recover_session,
            "sync_hub": self.sync_hub,
            "health_check": self.health_check,
            "get_server_capabilities": self.get_server_capabilities,
            "check_client_compatibility": self.check_client_compatibility,
            "list_tools": self.list_tool_definitions,
            "list_resources": self.list_resource_definitions,
            "generate_compact_context": self.generate_compact_context,
            "generate_compact_context_v2": self.generate_compact_context_v2,
            "generate_context_profile": self.generate_context_profile,
            "generate_delta_context": self.generate_delta_context,
            "generate_prompt_segments": self.generate_prompt_segments,
            "generate_retrieval_context": self.generate_retrieval_context,
            "generate_task_snapshot": self.generate_task_snapshot,
            "record_token_usage": self.record_token_usage,
            "get_token_usage_stats": self.get_token_usage_stats,
            "record_command_event": self.record_command_event,
            "record_command_batch": self.record_command_batch,
            "get_command_event": self.get_command_event,
            "get_recent_commands": self.get_recent_commands,
            "get_last_command_result": self.get_last_command_result,
            "get_command_failures": self.get_command_failures,
            "get_command_execution_policy": self.get_command_execution_policy,
            "get_output_response_policy": self.get_output_response_policy,
            "compact_tool_output": self.compact_tool_output,
            "compact_response": self.compact_response,
            "get_raw_output_capture": self.get_raw_output_capture,
            "get_fast_path_response": self.get_fast_path_response,
            "get_optimization_policy": self.get_optimization_policy,
            "list_context_chunks": self.list_context_chunks,
            "generate_progressive_context": self.generate_progressive_context,
            "generate_startup_context": self.generate_startup_context,
            "generate_startup_prompt_template": self.generate_startup_prompt_template,
            "set_current_task": self.set_current_task,
            "scan_codebase": self.scan_codebase,
            "get_code_atlas_status": self.get_code_atlas_status,
            "start_scan_job": self.start_scan_job,
            "get_scan_job": self.get_scan_job,
            "list_scan_jobs": self.list_scan_jobs,
            "wait_for_scan_job": self.wait_for_scan_job,
            "describe_module": self.describe_module,
            "describe_symbol": self.describe_symbol,
            "describe_feature": self.describe_feature,
            "web_search": self.web_search,
            "understand_image": self.understand_image,
            "search_code_knowledge": self.search_code_knowledge,
            "get_symbol_candidates": self.get_symbol_candidates,
            "get_related_symbols": self.get_related_symbols,
            "invalidate_semantic_cache": self.invalidate_semantic_cache,
            "refresh_semantic_description": self.refresh_semantic_description,
            # Phase 1: Task Templates
            "get_task_templates": self.get_task_templates,
            "get_task_template": self.get_task_template,
            "create_task_template": self.create_task_template,
            "delete_task_template": self.delete_task_template,
            "create_task_from_template": self.create_task_from_template,
            # Phase 1: Quick Log
            "quick_log": self.quick_log,
            # Phase 1: Audit Log
            "get_audit_log": self.get_audit_log,
            # Phase 2: Reset Project
            "reset_project": self.reset_project,
            # Phase 2: Bulk Task Operations
            "bulk_task_ops": self.bulk_task_ops,
            # Phase 2: Project Export
            "export_project": self.export_project,
            # Phase 3: Work Log Expiry
            "configure_log_expiry": self.configure_log_expiry,
            "expire_old_logs": self.expire_old_logs,
            "get_log_stats": self.get_log_stats,
            # Phase 3: Session Replay
            "session_replay": self.session_replay,
            # Phase 3: Task Dependencies
            "add_task_dependency": self.add_task_dependency,
            "remove_task_dependency": self.remove_task_dependency,
            "get_task_dependency": self.get_task_dependency,
            "get_all_dependencies": self.get_all_dependencies,
            "get_blocked_tasks": self.get_blocked_tasks,
            "validate_dependencies": self.validate_dependencies,
            # Milestone C: Token-saving retrieval
            "generate_fast_context": self.generate_fast_context,
            "retrieve_context_chunk": self.retrieve_context_chunk,
            # Milestone D: Cross-IDE / plugin handoff
            "generate_cross_tool_handoff": self.generate_cross_tool_handoff,
            "get_session_lineage_chain": self.get_session_lineage_chain,
            "set_session_environment": self.set_session_environment,
            # Milestone E: Automatic project resolution
            "get_or_create_project": self.get_or_create_project,
            "detect_project_type": self._detect_project_type,
            "scan_nearby_projects": self._scan_nearby_projects,
        }
        if name not in handlers:
            raise KeyError(f"Unknown tool: {name}")
        try:
            result = handlers[name](**arguments)
            log.info("tool_completed", tool_name=name)
            return result
        except Exception as exc:
            log.error("tool_failed", tool_name=name, error_type=type(exc).__name__, error_msg=str(exc))
            raise


TOOL_DEFINITIONS = [
    {"name": "register_project", "description": "Register a repo with obsmcp and create its centralized workspace.", "inputSchema": {"type": "object", "properties": {"repo_path": {"type": "string"}, "name": {"type": "string"}, "tags": {"type": "array", "items": {"type": "string"}}}, "required": ["repo_path"]}},
    {"name": "list_projects", "description": "List registered obsmcp projects.", "inputSchema": {"type": "object", "properties": {}}},
    {"name": "resolve_project", "description": "Resolve a project by slug or repo path.", "inputSchema": {"type": "object", "properties": {"project_slug": {"type": "string"}, "project_path": {"type": "string"}}}},
    {"name": "resolve_active_project", "description": "Resolve the active project from IDE metadata such as cwd, active file, workspace folders, open files, session_id, task_id, repo_path, or environment hints. Use this before the first continuity write from a plugin or IDE client.", "inputSchema": {"type": "object", "properties": {"project_path": {"type": "string"}, "project_slug": {"type": "string"}, "session_id": {"type": "string"}, "task_id": {"type": "string"}, "repo_path": {"type": "string"}, "cwd": {"type": "string"}, "workspace_path": {"type": "string"}, "workspace_folders": {"type": "array", "items": {"type": "string"}}, "active_file": {"type": "string"}, "open_files": {"type": "array", "items": {"type": "string"}}, "env_variables": {"type": "object", "additionalProperties": {"type": "string"}}, "auto_register": {"type": "boolean", "default": True}, "project_name": {"type": "string"}, "tags": {"type": "array", "items": {"type": "string"}}, "scan_nearby": {"type": "boolean", "default": False}, "ide_name": {"type": "string"}}, "required": []}},
    {"name": "get_project_workspace_paths", "description": "Return the workspace paths for a project.", "inputSchema": {"type": "object", "properties": {"project_slug": {"type": "string"}, "project_path": {"type": "string"}}}},
    {"name": "attach_repo_bridge", "description": "Write a lightweight bridge file into the repo that points at the centralized obsmcp workspace.", "inputSchema": {"type": "object", "properties": {"project_slug": {"type": "string"}, "project_path": {"type": "string"}}}},
    {"name": "migrate_project_layout", "description": "Copy legacy repo-local .context and obsidian/vault content into the centralized project workspace and attach a repo bridge.", "inputSchema": {"type": "object", "properties": {"project_slug": {"type": "string"}, "project_path": {"type": "string"}}}},
    {"name": "get_project_brief", "description": "Return the current project brief sections.", "inputSchema": {"type": "object", "properties": {"project_path": {"type": "string", "description": "Project root path. Defaults to OBSMCP_PROJECT env var or configured default."}}, "required": []}},
    {"name": "get_current_task", "description": "Return the current task.", "inputSchema": {"type": "object", "properties": {"project_path": {"type": "string", "description": "Project root path. Defaults to OBSMCP_PROJECT env var or configured default."}}, "required": []}},
    {"name": "get_active_tasks", "description": "Return open, in-progress, and blocked tasks.", "inputSchema": {"type": "object", "properties": {"project_path": {"type": "string", "description": "Project root path. Defaults to OBSMCP_PROJECT env var or configured default."}}, "required": []}},
    {"name": "get_latest_handoff", "description": "Return the latest handoff.", "inputSchema": {"type": "object", "properties": {"project_path": {"type": "string", "description": "Project root path. Defaults to OBSMCP_PROJECT env var or configured default."}}, "required": []}},
    {"name": "get_recent_work", "description": "Return recent work logs with cursor-style limit and after_id parameters.", "inputSchema": {"type": "object", "properties": {"limit": {"type": "integer", "default": 10, "description": "Maximum number of entries to return (default 10, max 1000)."}, "after_id": {"type": "integer", "description": "Return entries with id less than this value."}, "project_path": {"type": "string", "description": "Project root path. Defaults to OBSMCP_PROJECT env var or configured default."}}, "required": []}},
    {"name": "get_decisions", "description": "Return recent decisions with cursor-style limit and after_id parameters.", "inputSchema": {"type": "object", "properties": {"limit": {"type": "integer", "default": 10, "description": "Maximum number of entries to return (default 10, max 1000)."}, "after_id": {"type": "integer", "description": "Return entries with id less than this value."}, "project_path": {"type": "string", "description": "Project root path. Defaults to OBSMCP_PROJECT env var or configured default."}}, "required": []}},
    {"name": "get_blockers", "description": "Return open blockers with cursor-based pagination.", "inputSchema": {"type": "object", "properties": {"open_only": {"type": "boolean", "default": True, "description": "Only return open blockers (default true)."}, "limit": {"type": "integer", "default": 20, "description": "Maximum number of entries to return (default 20, max 1000)."}, "after_id": {"type": "integer", "description": "Return entries with id less than this value. Use next_cursor from the previous response to paginate."}, "project_path": {"type": "string", "description": "Project root path. Defaults to OBSMCP_PROJECT env var or configured default."}}, "required": []}},
    {"name": "get_relevant_files", "description": "Return relevant file paths for a task or the current task.", "inputSchema": {"type": "object", "properties": {"task_id": {"type": "string"}, "project_path": {"type": "string", "description": "Project root path. Defaults to OBSMCP_PROJECT env var or configured default."}}, "required": []}},
    {"name": "get_table_schema", "description": "Return the SQLite schema for a given table.", "inputSchema": {"type": "object", "properties": {"table_name": {"type": "string"}, "project_path": {"type": "string", "description": "Project root path. Defaults to OBSMCP_PROJECT env var or configured default."}}, "required": ["table_name"]}},
    {"name": "search_notes", "description": "Search the Obsidian vault for notes.", "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}, "limit": {"type": "integer"}, "project_path": {"type": "string", "description": "Project root path. Defaults to OBSMCP_PROJECT env var or configured default."}}, "required": ["query"]}},
    {"name": "read_note", "description": "Read a note from the Obsidian vault.", "inputSchema": {"type": "object", "properties": {"path": {"type": "string"}, "project_path": {"type": "string", "description": "Project root path. Defaults to OBSMCP_PROJECT env var or configured default."}}, "required": ["path"]}},
    {"name": "get_project_status_snapshot", "description": "Return a compact project status snapshot.", "inputSchema": {"type": "object", "properties": {"project_path": {"type": "string", "description": "Project root path. Defaults to OBSMCP_PROJECT env var or configured default."}}, "required": []}},
    {"name": "log_work", "description": "Append a work log entry.", "inputSchema": {"type": "object", "properties": {"message": {"type": "string"}, "summary": {"type": "string"}, "task_id": {"type": "string"}, "actor": {"type": "string"}, "session_id": {"type": "string"}, "files": {"type": "array", "items": {"type": "string"}}, "project_path": {"type": "string", "description": "Project root path. Defaults to OBSMCP_PROJECT env var or configured default."}}, "required": ["message"]}},
    {"name": "log_checkpoint", "description": "Record a completed checkpoint or subtask for a task.", "inputSchema": {"type": "object", "properties": {"task_id": {"type": "string"}, "checkpoint_id": {"type": "string"}, "title": {"type": "string"}, "message": {"type": "string"}, "status": {"type": "string", "default": "completed"}, "files": {"type": "array", "items": {"type": "string"}}, "actor": {"type": "string"}, "session_id": {"type": "string"}, "project_path": {"type": "string", "description": "Project root path. Defaults to OBSMCP_PROJECT env var or configured default."}}, "required": ["task_id", "checkpoint_id", "title"]}},
    {"name": "update_task", "description": "Update an existing task.", "inputSchema": {"type": "object", "properties": {"task_id": {"type": "string"}, "title": {"type": "string"}, "description": {"type": "string"}, "status": {"type": "string"}, "priority": {"type": "string"}, "owner": {"type": "string"}, "actor": {"type": "string"}, "session_id": {"type": "string"}, "relevant_files": {"type": "array", "items": {"type": "string"}}, "tags": {"type": "array", "items": {"type": "string"}}, "project_path": {"type": "string", "description": "Project root path. Defaults to OBSMCP_PROJECT env var or configured default."}}, "required": ["task_id"]}},
    {"name": "create_task", "description": "Create a task.", "inputSchema": {"type": "object", "properties": {"title": {"type": "string"}, "description": {"type": "string"}, "priority": {"type": "string"}, "owner": {"type": "string"}, "actor": {"type": "string"}, "session_id": {"type": "string"}, "relevant_files": {"type": "array", "items": {"type": "string"}}, "tags": {"type": "array", "items": {"type": "string"}}, "project_path": {"type": "string", "description": "Project root path. Defaults to OBSMCP_PROJECT env var or configured default."}}, "required": ["title", "description"]}},
    {"name": "get_task_progress", "description": "Return checkpoint progress and recent checkpoints for a task.", "inputSchema": {"type": "object", "properties": {"task_id": {"type": "string"}, "project_path": {"type": "string", "description": "Project root path. Defaults to OBSMCP_PROJECT env var or configured default."}}, "required": ["task_id"]}},
    {"name": "log_decision", "description": "Record an ADR-style decision.", "inputSchema": {"type": "object", "properties": {"title": {"type": "string"}, "decision": {"type": "string"}, "rationale": {"type": "string"}, "impact": {"type": "string"}, "task_id": {"type": "string"}, "actor": {"type": "string"}, "session_id": {"type": "string"}, "project_path": {"type": "string", "description": "Project root path. Defaults to OBSMCP_PROJECT env var or configured default."}}, "required": ["title", "decision"]}},
    {"name": "log_blocker", "description": "Record a blocker.", "inputSchema": {"type": "object", "properties": {"title": {"type": "string"}, "description": {"type": "string"}, "task_id": {"type": "string"}, "actor": {"type": "string"}, "session_id": {"type": "string"}, "project_path": {"type": "string", "description": "Project root path. Defaults to OBSMCP_PROJECT env var or configured default."}}, "required": ["title", "description"]}},
    {"name": "resolve_blocker", "description": "Resolve an open blocker.", "inputSchema": {"type": "object", "properties": {"blocker_id": {"type": "integer"}, "resolution_note": {"type": "string"}, "actor": {"type": "string"}, "project_path": {"type": "string", "description": "Project root path. Defaults to OBSMCP_PROJECT env var or configured default."}}, "required": ["blocker_id", "resolution_note"]}},
    {"name": "create_handoff", "description": "Create a model-to-model or user-to-model handoff.", "inputSchema": {"type": "object", "properties": {"summary": {"type": "string"}, "next_steps": {"type": "string"}, "open_questions": {"type": "string"}, "note": {"type": "string"}, "task_id": {"type": "string"}, "from_actor": {"type": "string"}, "to_actor": {"type": "string"}, "session_id": {"type": "string"}, "project_path": {"type": "string", "description": "Project root path. Defaults to OBSMCP_PROJECT env var or configured default."}}, "required": ["summary"]}},
    {"name": "append_handoff_note", "description": "Append an additional note to an existing handoff.", "inputSchema": {"type": "object", "properties": {"handoff_id": {"type": "integer"}, "note": {"type": "string"}, "actor": {"type": "string"}, "project_path": {"type": "string", "description": "Project root path. Defaults to OBSMCP_PROJECT env var or configured default."}}, "required": ["handoff_id", "note"]}},
    {"name": "update_project_brief_section", "description": "Update a named project brief section.", "inputSchema": {"type": "object", "properties": {"section": {"type": "string"}, "content": {"type": "string"}, "actor": {"type": "string"}, "session_id": {"type": "string"}, "project_path": {"type": "string", "description": "Project root path. Defaults to OBSMCP_PROJECT env var or configured default."}}, "required": ["section", "content"]}},
    {"name": "create_daily_note_entry", "description": "Append an entry to the daily note stream.", "inputSchema": {"type": "object", "properties": {"entry": {"type": "string"}, "note_date": {"type": "string"}, "actor": {"type": "string"}, "session_id": {"type": "string"}, "project_path": {"type": "string", "description": "Project root path. Defaults to OBSMCP_PROJECT env var or configured default."}}, "required": ["entry"]}},
    {"name": "sync_context_files", "description": "Force a sync of generated context and Obsidian files.", "inputSchema": {"type": "object", "properties": {"project_path": {"type": "string", "description": "Project root path. Defaults to OBSMCP_PROJECT env var or configured default."}}, "required": []}},
    {"name": "session_open", "description": "Open an auditable AI session with heartbeat and write-back policy. By default, obsmcp auto-resumes a recent matching open session for the same actor/client/project unless the mismatch guard blocks it.", "inputSchema": {"type": "object", "properties": {"actor": {"type": "string"}, "client_name": {"type": "string"}, "model_name": {"type": "string"}, "session_label": {"type": "string"}, "workstream_key": {"type": "string"}, "workstream_title": {"type": "string"}, "project_path": {"type": "string"}, "initial_request": {"type": "string"}, "session_goal": {"type": "string"}, "task_id": {"type": "string"}, "require_heartbeat": {"type": "boolean"}, "require_work_log": {"type": "boolean"}, "heartbeat_interval_seconds": {"type": "integer"}, "work_log_interval_seconds": {"type": "integer"}, "min_work_logs": {"type": "integer"}, "handoff_required": {"type": "boolean"}, "resume_strategy": {"type": "string", "enum": ["auto", "new", "resume"]}, "resume_session_id": {"type": "string"}}, "required": ["actor"]}},
    {"name": "session_heartbeat", "description": "Record a session heartbeat and optionally emit a heartbeat work log.", "inputSchema": {"type": "object", "properties": {"session_id": {"type": "string"}, "actor": {"type": "string"}, "status_note": {"type": "string"}, "task_id": {"type": "string"}, "files": {"type": "array", "items": {"type": "string"}}, "create_work_log": {"type": "boolean"}, "project_path": {"type": "string", "description": "Project root path. Defaults to OBSMCP_PROJECT env var or configured default."}}, "required": ["session_id", "actor"]}},
    {"name": "session_close", "description": "Close a session with summary and optional handoff creation.", "inputSchema": {"type": "object", "properties": {"session_id": {"type": "string"}, "actor": {"type": "string"}, "summary": {"type": "string"}, "create_handoff": {"type": "boolean"}, "handoff_summary": {"type": "string"}, "handoff_next_steps": {"type": "string"}, "handoff_open_questions": {"type": "string"}, "handoff_note": {"type": "string"}, "handoff_to_actor": {"type": "string"}, "project_path": {"type": "string", "description": "Project root path. Defaults to OBSMCP_PROJECT env var or configured default."}}, "required": ["session_id", "actor"]}},
    {"name": "get_active_sessions", "description": "List open tracked sessions with cursor-based pagination.", "inputSchema": {"type": "object", "properties": {"limit": {"type": "integer", "default": 50, "description": "Maximum number of sessions to return (default 50, max 1000)."}, "after_heartbeat_at": {"type": "string", "description": "Return sessions with heartbeat_at less than this value. Use next_cursor.heartbeat_at from previous response."}, "after_id": {"type": "string", "description": "Return sessions with id less than this value (tiebreaker when heartbeat_at equals after_heartbeat_at). Use next_cursor.id from previous response."}, "project_path": {"type": "string", "description": "Project root path. Defaults to OBSMCP_PROJECT env var or configured default."}}, "required": []}},
    {"name": "detect_missing_writeback", "description": "Audit sessions for missing write-back, missing handoffs, or overdue heartbeats.", "inputSchema": {"type": "object", "properties": {"include_closed": {"type": "boolean"}, "project_path": {"type": "string", "description": "Project root path. Defaults to OBSMCP_PROJECT env var or configured default."}}, "required": []}},
    {"name": "get_startup_preflight", "description": "Run startup safety checks before opening or resuming a session.", "inputSchema": {"type": "object", "properties": {"actor": {"type": "string"}, "task_id": {"type": "string"}, "session_id": {"type": "string"}, "initial_request": {"type": "string"}, "session_goal": {"type": "string"}, "session_label": {"type": "string"}, "workstream_key": {"type": "string"}, "client_name": {"type": "string"}, "model_name": {"type": "string"}, "project_path": {"type": "string", "description": "Project root path. Defaults to OBSMCP_PROJECT env var or configured default."}}, "required": []}},
    {"name": "get_resume_board", "description": "Return a startup dashboard of open tasks, paused tasks, stale sessions, latest handoffs, and the recommended resume target.", "inputSchema": {"type": "object", "properties": {"project_path": {"type": "string", "description": "Project root path. Defaults to OBSMCP_PROJECT env var or configured default."}}, "required": []}},
    {"name": "generate_resume_packet", "description": "Generate a compact resume packet for the next tool or model and write it to the project workspace.", "inputSchema": {"type": "object", "properties": {"session_id": {"type": "string"}, "task_id": {"type": "string"}, "project_path": {"type": "string"}, "write_files": {"type": "boolean"}}}},
    {"name": "generate_emergency_handoff", "description": "Generate a best-effort handoff from the latest persisted state when a session ended abruptly.", "inputSchema": {"type": "object", "properties": {"session_id": {"type": "string"}, "task_id": {"type": "string"}, "project_path": {"type": "string"}}}},
    {"name": "recover_session", "description": "Recover an interrupted session by generating an emergency handoff and resume packet.", "inputSchema": {"type": "object", "properties": {"session_id": {"type": "string"}, "actor": {"type": "string"}, "project_path": {"type": "string"}}}},
    {"name": "sync_hub", "description": "Refresh the central obsmcp hub vault from the registry.", "inputSchema": {"type": "object", "properties": {}}},
    {"name": "health_check", "description": "Return health information about obsmcp.", "inputSchema": {"type": "object", "properties": {}}},
    {"name": "get_server_capabilities", "description": "Return server API/schema versions and supported workflow-safety capabilities.", "inputSchema": {"type": "object", "properties": {"project_path": {"type": "string", "description": "Optional project root path."}}, "required": []}},
    {"name": "check_client_compatibility", "description": "Compare client API/tool-schema expectations with the current server.", "inputSchema": {"type": "object", "properties": {"client_api_version": {"type": "string"}, "client_tool_schema_version": {"type": "integer"}, "client_name": {"type": "string"}, "model_name": {"type": "string"}, "project_path": {"type": "string", "description": "Optional project root path."}}, "required": []}},
    {"name": "list_tools", "description": "Return the obsmcp tool catalog.", "inputSchema": {"type": "object", "properties": {}}},
    {"name": "list_resources", "description": "Return the obsmcp resource catalog.", "inputSchema": {"type": "object", "properties": {}}},
    {"name": "generate_compact_context", "description": "Generate compact context for manual prompt injection.", "inputSchema": {"type": "object", "properties": {"task_id": {"type": "string"}, "project_path": {"type": "string", "description": "Project root path. Defaults to OBSMCP_PROJECT env var or configured default."}}, "required": []}},
    {"name": "generate_compact_context_v2", "description": "Token-budget-aware compact context with decision chains, dependency map, session info, and smart truncation.", "inputSchema": {"type": "object", "properties": {"task_id": {"type": "string", "description": "Optional task ID to focus on."}, "max_tokens": {"type": "integer", "description": "Max tokens budget (default 3000).", "default": 3000}, "include_decision_chain": {"type": "boolean", "description": "Include decision chain section.", "default": True}, "include_dependency_map": {"type": "boolean", "description": "Include ASCII dependency map.", "default": True}, "include_session_info": {"type": "boolean", "description": "Include active session info.", "default": True}, "include_recent_work": {"type": "boolean", "description": "Include recent work log.", "default": True}, "include_daily_notes": {"type": "boolean", "description": "Include daily notes.", "default": False}, "project_path": {"type": "string", "description": "Project root path. Defaults to OBSMCP_PROJECT env var or configured default."}}, "required": []}},
    {"name": "generate_context_profile", "description": "Generate a cached tiered context profile such as fast, balanced, deep, handoff, or recovery.", "inputSchema": {"type": "object", "properties": {"profile": {"type": "string", "enum": ["fast", "balanced", "deep", "handoff", "recovery"], "default": "balanced"}, "task_id": {"type": "string"}, "max_tokens": {"type": "integer"}, "include_daily_notes": {"type": "boolean", "default": False}, "project_path": {"type": "string", "description": "Project root path. Defaults to OBSMCP_PROJECT env var or configured default."}}, "required": []}},
    {"name": "generate_delta_context", "description": "Generate a compact delta view showing what changed since a handoff, session, or timestamp.", "inputSchema": {"type": "object", "properties": {"task_id": {"type": "string"}, "since_handoff_id": {"type": "integer"}, "since_session_id": {"type": "string"}, "since_timestamp": {"type": "string"}, "project_path": {"type": "string", "description": "Project root path. Defaults to OBSMCP_PROJECT env var or configured default."}}, "required": []}},
    {"name": "generate_prompt_segments", "description": "Generate stable and dynamic prompt segments for prompt-cache-friendly context assembly.", "inputSchema": {"type": "object", "properties": {"profile": {"type": "string", "enum": ["fast", "balanced", "deep", "handoff", "recovery"], "default": "balanced"}, "task_id": {"type": "string"}, "max_tokens": {"type": "integer", "default": 2600}, "project_path": {"type": "string", "description": "Project root path. Defaults to OBSMCP_PROJECT env var or configured default."}}, "required": []}},
    {"name": "generate_retrieval_context", "description": "Generate retrieval-first context with ranked files, recent work, decisions, blockers, and semantic hits for a query.", "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}, "task_id": {"type": "string"}, "max_tokens": {"type": "integer", "default": 1800}, "include_delta": {"type": "boolean", "default": True}, "include_semantic": {"type": "boolean", "default": True}, "project_path": {"type": "string", "description": "Project root path. Defaults to OBSMCP_PROJECT env var or configured default."}}, "required": ["query"]}},
    {"name": "generate_task_snapshot", "description": "Generate a detailed snapshot for a task.", "inputSchema": {"type": "object", "properties": {"task_id": {"type": "string"}, "project_path": {"type": "string", "description": "Project root path. Defaults to OBSMCP_PROJECT env var or configured default."}}, "required": []}},
    {"name": "record_token_usage", "description": "Record provider or local token usage metrics, including prompt cache fields and compaction savings.", "inputSchema": {"type": "object", "properties": {"operation": {"type": "string"}, "event_type": {"type": "string", "default": "provider_usage"}, "actor": {"type": "string"}, "session_id": {"type": "string"}, "task_id": {"type": "string"}, "model_name": {"type": "string"}, "provider": {"type": "string"}, "client_name": {"type": "string"}, "raw_input_tokens": {"type": "integer"}, "raw_output_tokens": {"type": "integer"}, "estimated_input_tokens": {"type": "integer"}, "estimated_output_tokens": {"type": "integer"}, "compact_input_tokens": {"type": "integer"}, "compact_output_tokens": {"type": "integer"}, "saved_tokens": {"type": "integer"}, "cache_creation_input_tokens": {"type": "integer"}, "cache_read_input_tokens": {"type": "integer"}, "raw_chars": {"type": "integer"}, "compact_chars": {"type": "integer"}, "metadata": {"type": "object"}, "project_path": {"type": "string", "description": "Project root path. Defaults to OBSMCP_PROJECT env var or configured default."}}, "required": ["operation"]}},
    {"name": "get_token_usage_stats", "description": "Return recent token, compaction, and prompt-cache usage aggregates for the project.", "inputSchema": {"type": "object", "properties": {"limit": {"type": "integer", "default": 200}, "operation": {"type": "string"}, "session_id": {"type": "string"}, "project_path": {"type": "string", "description": "Project root path. Defaults to OBSMCP_PROJECT env var or configured default."}}, "required": []}},
    {"name": "record_command_event", "description": "Record a terminal command outcome with compact summaries and optional raw output capture for later retrieval.", "inputSchema": {"type": "object", "properties": {"command_text": {"type": "string"}, "actor": {"type": "string"}, "cwd": {"type": "string"}, "event_kind": {"type": "string", "default": "completed"}, "status": {"type": "string"}, "risk_level": {"type": "string", "default": "normal"}, "exit_code": {"type": "integer", "default": 0}, "duration_ms": {"type": "integer", "default": 0}, "output": {"type": "string", "description": "Combined command output to summarize if stdout/stderr are not provided."}, "stdout": {"type": "string"}, "stderr": {"type": "string"}, "summary": {"type": "string"}, "stdout_summary": {"type": "string"}, "stderr_summary": {"type": "string"}, "profile": {"type": "string"}, "policy_mode": {"type": "string", "enum": ["compact", "balanced", "debug", "recovery"], "default": "balanced"}, "files_changed": {"type": "array", "items": {"type": "string"}}, "capture_raw_on_failure": {"type": "boolean", "default": True}, "capture_raw_on_truncation": {"type": "boolean", "default": True}, "session_id": {"type": "string"}, "task_id": {"type": "string"}, "metadata": {"type": "object"}, "sync_mode": {"type": "string", "enum": ["full", "deferred", "none"], "default": "deferred"}, "project_path": {"type": "string", "description": "Project root path. Defaults to OBSMCP_PROJECT env var or configured default."}}, "required": ["command_text"]}},
    {"name": "record_command_batch", "description": "Record a batch of command outcomes and return an aggregate summary with risk counts.", "inputSchema": {"type": "object", "properties": {"commands": {"type": "array", "items": {"type": "object"}}, "actor": {"type": "string"}, "session_id": {"type": "string"}, "task_id": {"type": "string"}, "policy_mode": {"type": "string", "enum": ["compact", "balanced", "debug", "recovery"], "default": "balanced"}, "batch_label": {"type": "string"}, "sync_mode": {"type": "string", "enum": ["full", "deferred", "none"], "default": "deferred"}, "project_path": {"type": "string", "description": "Project root path. Defaults to OBSMCP_PROJECT env var or configured default."}}, "required": ["commands"]}},
    {"name": "get_command_event", "description": "Retrieve a recorded command event by ID.", "inputSchema": {"type": "object", "properties": {"event_id": {"type": "integer"}, "project_path": {"type": "string", "description": "Project root path. Defaults to OBSMCP_PROJECT env var or configured default."}}, "required": ["event_id"]}},
    {"name": "get_recent_commands", "description": "List recent recorded command events with cursor-based pagination, optionally filtered by session, task, status, or actor.", "inputSchema": {"type": "object", "properties": {"limit": {"type": "integer", "default": 20, "description": "Maximum entries to return (default 20, max 1000)."}, "after_id": {"type": "integer", "description": "Return entries with id less than this value. Use next_cursor from the previous response to paginate."}, "session_id": {"type": "string", "description": "Filter by session ID."}, "task_id": {"type": "string", "description": "Filter by task ID."}, "status": {"type": "string", "description": "Filter by status (completed, failed, etc.)."}, "actor": {"type": "string", "description": "Filter by actor."}, "project_path": {"type": "string", "description": "Project root path. Defaults to OBSMCP_PROJECT env var or configured default."}}, "required": []}},
    {"name": "get_last_command_result", "description": "Return the most recent recorded command event for a session or task.", "inputSchema": {"type": "object", "properties": {"session_id": {"type": "string"}, "task_id": {"type": "string"}, "actor": {"type": "string"}, "project_path": {"type": "string", "description": "Project root path. Defaults to OBSMCP_PROJECT env var or configured default."}}, "required": []}},
    {"name": "get_command_failures", "description": "List recent failing command events for a session or task.", "inputSchema": {"type": "object", "properties": {"limit": {"type": "integer", "default": 20}, "session_id": {"type": "string"}, "task_id": {"type": "string"}, "actor": {"type": "string"}, "project_path": {"type": "string", "description": "Project root path. Defaults to OBSMCP_PROJECT env var or configured default."}}, "required": []}},
    {"name": "get_command_execution_policy", "description": "Classify a command for batching and review risk, and combine that with the current optimization policy.", "inputSchema": {"type": "object", "properties": {"command": {"type": "string"}, "task_id": {"type": "string"}, "mode": {"type": "string", "enum": ["compact", "balanced", "debug", "recovery"], "default": "balanced"}, "exit_code": {"type": "integer", "default": 0}, "project_path": {"type": "string", "description": "Project root path. Defaults to OBSMCP_PROJECT env var or configured default."}}, "required": ["command"]}},
    {"name": "get_output_response_policy", "description": "Resolve the effective output-token policy for the current task/operation, including task overrides and safety bypasses.", "inputSchema": {"type": "object", "properties": {"task_id": {"type": "string"}, "operation_kind": {"type": "string", "default": "general", "enum": ["general", "review", "debugging", "architecture", "dangerous_actions", "security_sensitive", "legal_medical_financial", "ambiguity_clarification", "step_by_step_sensitive"]}, "detail_requested": {"type": "boolean", "default": False}, "command": {"type": "string"}, "project_path": {"type": "string", "description": "Project root path. Defaults to OBSMCP_PROJECT env var or configured default."}}, "required": []}},
    {"name": "compact_tool_output", "description": "Apply RTK-style output compaction to noisy tool output and optionally save the full raw output for debugging.", "inputSchema": {"type": "object", "properties": {"command": {"type": "string"}, "output": {"type": "string"}, "exit_code": {"type": "integer", "default": 0}, "profile": {"type": "string"}, "policy_mode": {"type": "string", "enum": ["compact", "balanced", "debug", "recovery"], "default": "balanced"}, "actor": {"type": "string"}, "session_id": {"type": "string"}, "task_id": {"type": "string"}, "capture_raw_on_failure": {"type": "boolean", "default": True}, "capture_raw_on_truncation": {"type": "boolean", "default": True}, "project_path": {"type": "string", "description": "Project root path. Defaults to OBSMCP_PROJECT env var or configured default."}}, "required": ["command", "output"]}},
    {"name": "compact_response", "description": "Compress text output using rule-based patterns to reduce output tokens. Preserves code blocks, URLs, filepaths, and error messages. Use this to reduce token costs on verbose AI responses.", "inputSchema": {"type": "object", "properties": {"text": {"type": "string", "description": "The text to compress."}, "level": {"type": "string", "enum": ["lite", "full", "ultra"], "default": "full", "description": "Compression level: lite (minimal), full (default, recommended), ultra (maximum)."}}, "required": ["text"]}},
    {"name": "get_raw_output_capture", "description": "Retrieve metadata or full content for a previously saved raw tool output capture.", "inputSchema": {"type": "object", "properties": {"capture_id": {"type": "string"}, "include_content": {"type": "boolean", "default": False}, "project_path": {"type": "string", "description": "Project root path. Defaults to OBSMCP_PROJECT env var or configured default."}}, "required": ["capture_id"]}},
    {"name": "get_fast_path_response", "description": "Return a deterministic no-LLM fast-path response such as current task, blockers, relevant files, project status, startup/resume packets, startup preflight, resume board, retrieval context, command history lookups, or semantic lookup.", "inputSchema": {"type": "object", "properties": {"kind": {"type": "string", "enum": ["current_task", "blockers", "relevant_files", "task_snapshot", "project_status", "resume_packet", "startup_context", "startup_preflight", "resume_board", "recent_commands", "last_command", "command_failures", "retrieval", "semantic_lookup"]}, "task_id": {"type": "string"}, "session_id": {"type": "string"}, "module_path": {"type": "string"}, "symbol_name": {"type": "string"}, "feature_name": {"type": "string"}, "query": {"type": "string"}, "as_markdown": {"type": "boolean", "default": False}, "project_path": {"type": "string", "description": "Project root path. Defaults to OBSMCP_PROJECT env var or configured default."}}, "required": ["kind"]}},
    {"name": "get_optimization_policy", "description": "Return the active adaptive optimization policy for a mode, task, command, and exit state.", "inputSchema": {"type": "object", "properties": {"mode": {"type": "string", "enum": ["compact", "balanced", "debug", "recovery"], "default": "balanced"}, "task_id": {"type": "string"}, "command": {"type": "string"}, "exit_code": {"type": "integer", "default": 0}, "project_path": {"type": "string", "description": "Project root path. Defaults to OBSMCP_PROJECT env var or configured default."}}, "required": []}},
    {"name": "list_context_chunks", "description": "List prioritized chunk metadata for a context artifact to support progressive loading.", "inputSchema": {"type": "object", "properties": {"artifact_type": {"type": "string", "enum": ["context_profile", "delta_context", "prompt_segments", "retrieval_context", "resume_packet"], "default": "context_profile"}, "profile": {"type": "string", "default": "deep"}, "task_id": {"type": "string"}, "query": {"type": "string"}, "project_path": {"type": "string", "description": "Project root path. Defaults to OBSMCP_PROJECT env var or configured default."}}, "required": []}},
    {"name": "generate_progressive_context", "description": "Render one or more prioritized chunks from a context artifact with navigation metadata for progressive loading.", "inputSchema": {"type": "object", "properties": {"artifact_type": {"type": "string", "enum": ["context_profile", "delta_context", "prompt_segments", "retrieval_context", "resume_packet"], "default": "context_profile"}, "profile": {"type": "string", "default": "deep"}, "start_chunk": {"type": "integer", "default": 0}, "chunk_count": {"type": "integer", "default": 2}, "task_id": {"type": "string"}, "query": {"type": "string"}, "project_path": {"type": "string", "description": "Project root path. Defaults to OBSMCP_PROJECT env var or configured default."}}, "required": []}},
    {"name": "generate_startup_context", "description": "Generate a delta-first startup context with fast baseline, recent command history, and execution policy hints.", "inputSchema": {"type": "object", "properties": {"task_id": {"type": "string"}, "session_id": {"type": "string"}, "max_tokens": {"type": "integer", "default": 1800}, "prefer_cached_delta": {"type": "boolean", "default": True}, "project_path": {"type": "string", "description": "Project root path. Defaults to OBSMCP_PROJECT env var or configured default."}}, "required": []}},
    {"name": "generate_startup_prompt_template", "description": "Return the first-contact startup prompt template for tools and agents.", "inputSchema": {"type": "object", "properties": {"first_contact": {"type": "boolean"}}}},
    {"name": "set_current_task", "description": "Set the current active task.", "inputSchema": {"type": "object", "properties": {"task_id": {"type": "string"}, "actor": {"type": "string"}, "session_id": {"type": "string"}, "project_path": {"type": "string", "description": "Project root path. Defaults to OBSMCP_PROJECT env var or configured default."}}, "required": ["task_id"]}},
    {"name": "scan_codebase", "description": "Scan the project directory and generate a Code Atlas documenting every file, function, class, and feature. Supports Python, JS/TS, Rust, Java, C/C++, Go, HTML, CSS, and more. When called over MCP, scans default to background mode so clients can poll instead of timing out.", "inputSchema": {"type": "object", "properties": {"force_refresh": {"type": "boolean", "description": "If True, always regenerate the atlas. If False (default), only regenerate if any source file is newer than the existing atlas."}, "background": {"type": "boolean", "description": "If True, queue the scan as a background job and return a job record instead of blocking."}, "requested_by": {"type": "string", "description": "Optional actor label for the queued job."}, "project_path": {"type": "string", "description": "Project root path. Defaults to OBSMCP_PROJECT env var or configured default."}}, "required": []}},
    {"name": "get_code_atlas_status", "description": "Return the current status of the Code Atlas without regenerating it. Tells you if the atlas exists, when it was last generated, total files, total lines, and languages found.", "inputSchema": {"type": "object", "properties": {"project_path": {"type": "string", "description": "Project root path. Defaults to OBSMCP_PROJECT env var or configured default."}}, "required": []}},
    {"name": "start_scan_job", "description": "Queue a background code atlas scan job and return its job ID for polling.", "inputSchema": {"type": "object", "properties": {"force_refresh": {"type": "boolean"}, "requested_by": {"type": "string"}, "project_path": {"type": "string", "description": "Project root path. Defaults to OBSMCP_PROJECT env var or configured default."}}, "required": []}},
    {"name": "get_scan_job", "description": "Get the current status and result payload for a background scan job.", "inputSchema": {"type": "object", "properties": {"job_id": {"type": "string"}, "project_path": {"type": "string", "description": "Project root path. Defaults to OBSMCP_PROJECT env var or configured default."}}, "required": ["job_id"]}},
    {"name": "list_scan_jobs", "description": "List recent background scan jobs for the project.", "inputSchema": {"type": "object", "properties": {"status": {"type": "string", "enum": ["queued", "running", "completed", "failed", "interrupted"]}, "limit": {"type": "integer"}, "project_path": {"type": "string", "description": "Project root path. Defaults to OBSMCP_PROJECT env var or configured default."}}}},
    {"name": "wait_for_scan_job", "description": "Poll a background scan job until it completes or the wait timeout elapses.", "inputSchema": {"type": "object", "properties": {"job_id": {"type": "string"}, "wait_seconds": {"type": "integer"}, "poll_interval_seconds": {"type": "number"}, "project_path": {"type": "string", "description": "Project root path. Defaults to OBSMCP_PROJECT env var or configured default."}}, "required": ["job_id"]}},
    {"name": "describe_module", "description": "Return a cached or freshly generated semantic description for a module/file.", "inputSchema": {"type": "object", "properties": {"module_path": {"type": "string"}, "force_llm": {"type": "boolean", "default": False, "description": "Force LLM-powered generation even if cache is valid."}, "project_path": {"type": "string", "description": "Project root path. Defaults to OBSMCP_PROJECT env var or configured default."}}, "required": ["module_path"]}},
    {"name": "describe_symbol", "description": "Return a semantic description for a function or class. If multiple candidates exist, returns an ambiguity payload.", "inputSchema": {"type": "object", "properties": {"symbol_name": {"type": "string"}, "module_path": {"type": "string"}, "entity_key": {"type": "string"}, "entity_type": {"type": "string", "enum": ["function", "class"]}, "force_llm": {"type": "boolean", "default": False, "description": "Force LLM-powered generation even if cache is valid."}, "project_path": {"type": "string", "description": "Project root path. Defaults to OBSMCP_PROJECT env var or configured default."}}}},
    {"name": "describe_feature", "description": "Return a semantic description for a feature tag from the Code Atlas.", "inputSchema": {"type": "object", "properties": {"feature_name": {"type": "string"}, "force_llm": {"type": "boolean", "default": False, "description": "Force LLM-powered generation even if cache is valid."}, "project_path": {"type": "string", "description": "Project root path. Defaults to OBSMCP_PROJECT env var or configured default."}}, "required": ["feature_name"]}},
    {"name": "web_search", "description": "Run a web search through obsmcp using the configured OpusMax tool provider.", "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}, "max_results": {"type": "integer"}, "actor": {"type": "string"}, "session_id": {"type": "string"}, "task_id": {"type": "string"}, "client_name": {"type": "string"}, "project_path": {"type": "string", "description": "Project root path. Defaults to OBSMCP_PROJECT env var or configured default."}}, "required": ["query"]}},
    {"name": "understand_image", "description": "Analyze an image through obsmcp using the configured OpusMax image-understanding tool provider.", "inputSchema": {"type": "object", "properties": {"prompt": {"type": "string"}, "image_url": {"type": "string", "description": "HTTP(S) URL, data URL, or existing local file path."}, "image_path": {"type": "string"}, "image_base64": {"type": "string"}, "mime_type": {"type": "string"}, "actor": {"type": "string"}, "session_id": {"type": "string"}, "task_id": {"type": "string"}, "client_name": {"type": "string"}, "project_path": {"type": "string", "description": "Project root path. Defaults to OBSMCP_PROJECT env var or configured default."}}, "required": ["prompt"]}},
    {"name": "search_code_knowledge", "description": "Search semantic knowledge and symbol index entries.", "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}, "limit": {"type": "integer"}, "project_path": {"type": "string", "description": "Project root path. Defaults to OBSMCP_PROJECT env var or configured default."}}, "required": ["query"]}},
    {"name": "get_symbol_candidates", "description": "Return matching function/class symbol candidates for a name.", "inputSchema": {"type": "object", "properties": {"symbol_name": {"type": "string"}, "module_path": {"type": "string"}, "entity_type": {"type": "string", "enum": ["function", "class"]}, "limit": {"type": "integer"}, "project_path": {"type": "string", "description": "Project root path. Defaults to OBSMCP_PROJECT env var or configured default."}}, "required": ["symbol_name"]}},
    {"name": "get_related_symbols", "description": "Return nearby or feature-related symbols for a semantic entity.", "inputSchema": {"type": "object", "properties": {"entity_key": {"type": "string"}, "limit": {"type": "integer"}, "project_path": {"type": "string", "description": "Project root path. Defaults to OBSMCP_PROJECT env var or configured default."}}, "required": ["entity_key"]}},
    {"name": "invalidate_semantic_cache", "description": "Mark semantic description cache entries stale by entity or file.", "inputSchema": {"type": "object", "properties": {"entity_key": {"type": "string"}, "file_paths": {"type": "array", "items": {"type": "string"}}, "project_path": {"type": "string", "description": "Project root path. Defaults to OBSMCP_PROJECT env var or configured default."}}}},
    {"name": "refresh_semantic_description", "description": "Force a fresh semantic description generation for an entity lookup.", "inputSchema": {"type": "object", "properties": {"entity_key": {"type": "string"}, "module_path": {"type": "string"}, "symbol_name": {"type": "string"}, "feature_name": {"type": "string"}, "entity_type": {"type": "string", "enum": ["function", "class"]}, "force_llm": {"type": "boolean", "default": False, "description": "Use LLM-powered generation when refreshing."}, "project_path": {"type": "string", "description": "Project root path. Defaults to OBSMCP_PROJECT env var or configured default."}}}},
    # Phase 1: Task Templates
    {"name": "get_task_templates", "description": "List all available task templates.", "inputSchema": {"type": "object", "properties": {"project_path": {"type": "string", "description": "Project root path. Defaults to OBSMCP_PROJECT env var or configured default."}}, "required": []}},
    {"name": "get_task_template", "description": "Get a specific task template by name.", "inputSchema": {"type": "object", "properties": {"name": {"type": "string"}, "project_path": {"type": "string", "description": "Project root path. Defaults to OBSMCP_PROJECT env var or configured default."}}, "required": ["name"]}},
    {"name": "create_task_template", "description": "Create a new task template.", "inputSchema": {"type": "object", "properties": {"name": {"type": "string"}, "title_template": {"type": "string"}, "description_template": {"type": "string"}, "priority": {"type": "string"}, "tags": {"type": "array", "items": {"type": "string"}}, "project_path": {"type": "string", "description": "Project root path. Defaults to OBSMCP_PROJECT env var or configured default."}}, "required": ["name", "title_template", "description_template"]}},
    {"name": "delete_task_template", "description": "Delete a task template by name.", "inputSchema": {"type": "object", "properties": {"name": {"type": "string"}, "project_path": {"type": "string", "description": "Project root path. Defaults to OBSMCP_PROJECT env var or configured default."}}, "required": []}},
    {"name": "create_task_from_template", "description": "Create a task from a named template, filling in template variables.", "inputSchema": {"type": "object", "properties": {"template_name": {"type": "string"}, "variables": {"type": "object", "additionalProperties": {"type": "string"}}, "actor": {"type": "string"}, "session_id": {"type": "string"}, "project_path": {"type": "string", "description": "Project root path. Defaults to OBSMCP_PROJECT env var or configured default."}}, "required": ["template_name"]}},
    # Phase 1: Quick Log
    {"name": "quick_log", "description": "One-liner work log that auto-tags the current task. No task_id required — uses the current task from project state.", "inputSchema": {"type": "object", "properties": {"message": {"type": "string"}, "files": {"type": "array", "items": {"type": "string"}}, "actor": {"type": "string"}, "session_id": {"type": "string"}, "project_path": {"type": "string", "description": "Project root path. Defaults to OBSMCP_PROJECT env var or configured default."}}, "required": ["message"]}},
    # Phase 1: Audit Log
    {"name": "get_audit_log", "description": "Full project-wide activity timeline with cursor-based pagination. Shows every action performed, by whom, when, and on which task.", "inputSchema": {"type": "object", "properties": {"actor": {"type": "string", "description": "Filter by actor."}, "task_id": {"type": "string", "description": "Filter by task."}, "action_type": {"type": "string", "description": "Filter by action type."}, "from_date": {"type": "string", "description": "Filter entries from this date (ISO8601)."}, "to_date": {"type": "string", "description": "Filter entries up to this date (ISO8601)."}, "limit": {"type": "integer", "default": 100, "description": "Maximum entries to return (default 100, max 1000)."}, "after_id": {"type": "integer", "description": "Return entries with id less than this value. Use next_cursor from the previous response to paginate."}, "include_ai_only": {"type": "boolean", "default": False, "description": "Exclude ctx/manual/human actors."}, "project_path": {"type": "string", "description": "Project root path. Defaults to OBSMCP_PROJECT env var or configured default."}}}},
    # Phase 2: Reset Project
    {"name": "reset_project", "description": "Wipe project data by scope. WARNING: This permanently deletes data. Always creates an audit trail before wiping. Valid scopes: tasks, blockers, sessions, work_logs, decisions, handoffs, full.", "inputSchema": {"type": "object", "properties": {"scope": {"type": "string", "enum": ["tasks", "blockers", "sessions", "work_logs", "decisions", "handoffs", "full"]}, "actor": {"type": "string"}, "project_path": {"type": "string", "description": "Project root path. Defaults to OBSMCP_PROJECT env var or configured default."}}, "required": ["scope"]}},
    # Phase 2: Bulk Task Operations
    {"name": "bulk_task_ops", "description": "Execute multiple task operations atomically. All succeed or all fail. Actions: create, update, close, delete, set_current.", "inputSchema": {"type": "object", "properties": {"operations": {"type": "array", "items": {"type": "object"}}, "actor": {"type": "string"}, "project_path": {"type": "string", "description": "Project root path. Defaults to OBSMCP_PROJECT env var or configured default."}}, "required": ["operations"]}},
    # Phase 2: Project Export
    {"name": "export_project", "description": "Export full project state as JSON (gzipped) and/or Markdown bundle. Creates a timestamped export in data/exports/.", "inputSchema": {"type": "object", "properties": {"format": {"type": "string", "enum": ["json", "markdown", "both"]}, "project_path": {"type": "string", "description": "Project root path. Defaults to OBSMCP_PROJECT env var or configured default."}}}},
    # Phase 3: Work Log Expiry
    {"name": "configure_log_expiry", "description": "Set the work log retention period in days. Set to 0 to disable. Logs older than N days are purged on expire_old_logs() or auto-trigger.", "inputSchema": {"type": "object", "properties": {"days": {"type": "integer"}, "actor": {"type": "string"}, "project_path": {"type": "string", "description": "Project root path. Defaults to OBSMCP_PROJECT env var or configured default."}}, "required": ["days"]}},
    {"name": "expire_old_logs", "description": "Purge work logs older than the configured retention period. Records a decision entry. Never deletes logs from open sessions.", "inputSchema": {"type": "object", "properties": {"actor": {"type": "string"}, "project_path": {"type": "string", "description": "Project root path. Defaults to OBSMCP_PROJECT env var or configured default."}}}},
    {"name": "get_log_stats", "description": "Return work log statistics: total count, age buckets (today/this week/this month/last 3 months/older), and current expiry setting.", "inputSchema": {"type": "object", "properties": {"project_path": {"type": "string", "description": "Project root path. Defaults to OBSMCP_PROJECT env var or configured default."}}}},
    # Phase 3: Session Replay
    {"name": "session_replay", "description": "Reconstruct the full timeline of events within a session. If no session_id is provided, uses the most recent session. Returns events, statistics, warnings, and a rendered Markdown timeline.", "inputSchema": {"type": "object", "properties": {"session_id": {"type": "string"}, "project_path": {"type": "string", "description": "Project root path. Defaults to OBSMCP_PROJECT env var or configured default."}}}},
    # Phase 3: Task Dependencies
    {"name": "add_task_dependency", "description": "Link a task as blocked by other tasks and/or blocking other tasks. Automatically detects and rejects circular dependencies.", "inputSchema": {"type": "object", "properties": {"task_id": {"type": "string"}, "blocked_by": {"type": "array", "items": {"type": "string"}}, "blocks": {"type": "array", "items": {"type": "string"}}, "project_path": {"type": "string", "description": "Project root path. Defaults to OBSMCP_PROJECT env var or configured default."}}, "required": ["task_id"]}},
    {"name": "remove_task_dependency", "description": "Remove task dependencies. Provide blocked_by or blocks lists to selectively remove specific links.", "inputSchema": {"type": "object", "properties": {"task_id": {"type": "string"}, "blocked_by": {"type": "array", "items": {"type": "string"}}, "blocks": {"type": "array", "items": {"type": "string"}}, "project_path": {"type": "string", "description": "Project root path. Defaults to OBSMCP_PROJECT env var or configured default."}}, "required": ["task_id"]}},
    {"name": "get_task_dependency", "description": "Get dependencies for a specific task.", "inputSchema": {"type": "object", "properties": {"task_id": {"type": "string"}, "project_path": {"type": "string", "description": "Project root path. Defaults to OBSMCP_PROJECT env var or configured default."}}, "required": ["task_id"]}},
    {"name": "get_all_dependencies", "description": "Get all task dependencies across the project.", "inputSchema": {"type": "object", "properties": {"project_path": {"type": "string", "description": "Project root path. Defaults to OBSMCP_PROJECT env var or configured default."}}}},
    {"name": "get_blocked_tasks", "description": "Return tasks that are currently blocked by unresolved dependencies.", "inputSchema": {"type": "object", "properties": {"project_path": {"type": "string", "description": "Project root path. Defaults to OBSMCP_PROJECT env var or configured default."}}}},
    {"name": "validate_dependencies", "description": "Validate all task dependencies. Checks for circular dependencies and broken task references.", "inputSchema": {"type": "object", "properties": {"project_path": {"type": "string", "description": "Project root path. Defaults to OBSMCP_PROJECT env var or configured default."}}}},
    # Milestone C: Token-saving retrieval
    {"name": "generate_fast_context", "description": "Generate a guaranteed-fast L0-only context for startup/resume use cases. Returns only mission, current task, relevant files, latest handoff, and blockers — no semantic lookups, dependency map, daily notes, or audit. Ephemeral (no artifact written).", "inputSchema": {"type": "object", "properties": {"task_id": {"type": "string"}, "project_path": {"type": "string", "description": "Project root path. Defaults to OBSMCP_PROJECT env var or configured default."}}, "required": []}},
    {"name": "retrieve_context_chunk", "description": "Retrieve a specific chunk of a context artifact for large profile navigation. If chunk not cached, generates full artifact, splits by section priority, and caches each chunk.", "inputSchema": {"type": "object", "properties": {"artifact_type": {"type": "string", "enum": ["context_profile", "delta_context", "prompt_segments", "retrieval_context", "resume_packet"]}, "chunk_index": {"type": "integer", "minimum": 0}, "profile": {"type": "string"}, "task_id": {"type": "string"}, "query": {"type": "string"}, "project_path": {"type": "string", "description": "Project root path. Defaults to OBSMCP_PROJECT env var or configured default."}}, "required": []}},
    # Milestone D: Cross-IDE / plugin handoff
    {"name": "generate_cross_tool_handoff", "description": "Generate a structured JSON handoff payload targeting a specific tool or environment (e.g., claude-code, vscode, jetbrains). Includes task state, recent decisions, blockers, relevant files, session lineage chain, and IDE environment metadata.", "inputSchema": {"type": "object", "properties": {"handoff_id": {"type": "integer"}, "session_id": {"type": "string"}, "target_tool": {"type": "string", "description": "Target tool identifier (e.g., claude-code, vscode, jetbrains)."}, "target_env": {"type": "string", "description": "Target environment label (e.g., default, production, testing)."}, "project_path": {"type": "string", "description": "Project root path."}}, "required": []}},
    {"name": "get_session_lineage_chain", "description": "Traverse the session lineage chain from a given session to its root ancestor, showing parent sessions, actors, depth, and timestamps.", "inputSchema": {"type": "object", "properties": {"session_id": {"type": "string"}, "max_depth": {"type": "integer", "minimum": 1, "maximum": 20}, "project_path": {"type": "string", "description": "Project root path."}}, "required": ["session_id"]}},
    {"name": "set_session_environment", "description": "Attach IDE/environment metadata to an active session and optionally establish its parent lineage. Use this when switching IDEs or resuming across environments.", "inputSchema": {"type": "object", "properties": {"session_id": {"type": "string"}, "ide_name": {"type": "string"}, "ide_version": {"type": "string"}, "ide_platform": {"type": "string"}, "os_name": {"type": "string"}, "os_version": {"type": "string"}, "env_variables": {"type": "object", "additionalProperties": {"type": "string"}}, "startup_context": {"type": "object"}, "parent_session_id": {"type": "string"}, "project_path": {"type": "string", "description": "Project root path."}}, "required": ["session_id"]}},
    # Milestone E: Automatic project resolution
    {"name": "get_or_create_project", "description": "Auto-detect or create a project from a path hint, session, task, or environment. Resolves from multiple sources and optionally registers if not known. Returns project type metadata, workspace type, and nearby projects.", "inputSchema": {"type": "object", "properties": {"project_path": {"type": "string", "description": "Project root path."}, "session_id": {"type": "string"}, "task_id": {"type": "string"}, "auto_register": {"type": "boolean"}, "project_name": {"type": "string"}, "tags": {"type": "array", "items": {"type": "string"}}, "scan_nearby": {"type": "boolean"}}, "required": []}},
]
