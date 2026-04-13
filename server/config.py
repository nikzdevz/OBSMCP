from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from .projects import project_slug_for_path


@dataclass
class ObsidianConfig:
    project_brief_note: str
    current_task_note: str
    status_snapshot_note: str
    latest_handoff_note: str
    decision_index_note: str
    daily_notes_dir: str
    session_note: str
    code_atlas_note: str


@dataclass
class LoggingConfig:
    """Structured observability configuration."""
    level: str = "INFO"  # DEBUG, INFO, WARNING, ERROR
    json_output: bool = True
    json_output_path: str = "obsmcp-structured.json"
    include_traceback: bool = False
    console_output: bool = True


@dataclass
class CheckpointConfig:
    enabled: bool = True
    render_limit: int = 12
    auto_rollup: bool = False
    auto_close_task: bool = False


@dataclass
class PromptOnlyConfig:
    enabled: bool = True
    direct_answer_first: bool = True
    no_greetings: bool = True
    no_recap: bool = True
    short_paragraphs: bool = True
    prefer_bullets_for_lists: bool = True
    max_paragraph_sentences: int = 3
    findings_first_for_reviews: bool = True


@dataclass
class GatewayEnforcedConfig:
    enabled: bool = False
    inject_contract: bool = True
    enforce_direct_answer_first: bool = True
    enforce_findings_first_for_reviews: bool = True
    max_output_tokens_soft: int = 900
    max_output_sections: int = 6
    max_paragraph_lines: int = 4


@dataclass
class SafetyBypassConfig:
    enabled: bool = True
    destructive_actions: bool = True
    security_sensitive: bool = True
    legal_medical_financial: bool = True
    ambiguity_clarification: bool = True
    step_by_step_sensitive: bool = True


@dataclass
class OutputObservabilityConfig:
    log_metrics: bool = True
    record_mode: bool = True
    record_style: bool = True
    record_task_type: bool = True
    sample_rate: float = 1.0


@dataclass
class TaskOutputOverrideConfig:
    mode: str | None = None
    style: str | None = None
    level: str | None = None


def _default_preserve_patterns() -> dict[str, bool]:
    return {
        "code_blocks": True,
        "urls": True,
        "filepaths": True,
        "error_messages": True,
        "json_output": True,
        "commands": True,
        "stack_traces": True,
    }


@dataclass
class OutputCompressionConfig:
    enabled: bool = False
    mode: str = "off"
    level: str = "full"
    style: str = "concise_professional"
    respect_user_detail_requests: bool = True
    expand_on_request: bool = True
    task_overrides: dict[str, TaskOutputOverrideConfig] = field(default_factory=dict)
    preserve_patterns: dict[str, bool] = field(default_factory=_default_preserve_patterns)
    prompt_only: PromptOnlyConfig = field(default_factory=PromptOnlyConfig)
    gateway_enforced: GatewayEnforcedConfig = field(default_factory=GatewayEnforcedConfig)
    safety_bypass: SafetyBypassConfig = field(default_factory=SafetyBypassConfig)
    observability: OutputObservabilityConfig = field(default_factory=OutputObservabilityConfig)


@dataclass
class SemanticAutoGenerateConfig:
    enabled: bool = True
    allow_llm: bool = False
    max_modules_per_scan: int = 5
    max_modules_per_write: int = 3
    on_log_work: bool = True
    on_update_task: bool = True
    on_create_task: bool = True
    on_set_current_task: bool = True
    on_handoff: bool = True
    on_startup: bool = True
    max_queue_size: int = 8
    max_concurrent_jobs: int = 1
    wait_ms_on_handoff: int = 250
    wait_ms_on_startup: int = 150
    skip_path_fragments: list[str] = field(
        default_factory=lambda: [
            "/.git/",
            "/.hg/",
            "/.svn/",
            "/.venv/",
            "/venv/",
            "/node_modules/",
            "/dist/",
            "/build/",
            "/target/",
            "/vendor/",
            "/__pycache__/",
            "/coverage/",
            "/.next/",
        ]
    )
    skip_generated_suffixes: list[str] = field(
        default_factory=lambda: [
            ".min.js",
            ".bundle.js",
            ".generated.ts",
            ".generated.js",
            ".generated.py",
            ".pb.go",
            ".designer.cs",
        ]
    )


@dataclass
class ProjectConfig:
    """Per-project configuration — paths derived from a specific project workspace."""
    project_slug: str
    project_name: str
    project_path: Path
    workspace_root: Path
    data_root: Path
    db_path: Path
    json_export_dir: Path
    backup_dir: Path
    export_dir: Path
    log_dir: Path
    vault_path: Path
    context_path: Path
    sessions_path: Path
    manifest_path: Path
    bridge_file_path: Path


@dataclass
class AppConfig:
    root_dir: Path
    app_name: str
    description: str
    host: str
    port: int
    bind_local_only: bool
    database_path: Path
    json_export_dir: Path
    backup_dir: Path
    log_dir: Path
    context_dir: Path
    obsidian_vault_dir: Path
    pid_file: Path
    max_recent_work_items: int
    max_decisions: int
    max_blockers: int
    strict_project_routing: bool = True
    bootstrap_default_project_on_startup: bool = False
    api_token: str | None = None
    obsidian: ObsidianConfig | None = None
    default_project_path: Path | None = None
    workspace_root_dir: Path | None = None
    projects_root_dir: Path | None = None
    hub_vault_dir: Path | None = None
    registry_path: Path | None = None
    repo_bridge_filename: str = ".obsmcp-link.json"
    logging: LoggingConfig | None = None
    checkpoints: CheckpointConfig = field(default_factory=CheckpointConfig)
    semantic_auto_generate: SemanticAutoGenerateConfig = field(default_factory=SemanticAutoGenerateConfig)
    output_compression: OutputCompressionConfig = field(default_factory=OutputCompressionConfig)

    def ensure_directories(self) -> None:
        base_paths = {
            self.database_path.parent,
            self.json_export_dir,
            self.backup_dir,
            self.log_dir,
            self.context_dir,
            self.obsidian_vault_dir,
            self.pid_file.parent,
            self.projects_root_dir or self.root_dir / "projects",
            (self.registry_path or self.root_dir / "registry" / "projects.json").parent,
        }
        paths: set[Path] = set()
        for path in base_paths:
            current = path
            while True:
                paths.add(current)
                if current == self.root_dir or current.parent == current:
                    break
                current = current.parent
        for path in sorted(paths, key=lambda item: (len(item.parts), str(item))):
            if path.exists():
                continue
            path.mkdir(exist_ok=True)

    def note_path(self, relative_path: str) -> Path:
        return self.obsidian_vault_dir / relative_path

    def get_project_config(self, project_path: str | Path | None, project_slug: str | None = None) -> ProjectConfig:
        if project_path:
            base = Path(project_path).resolve()
        else:
            env_project = os.environ.get("OBSMCP_PROJECT")
            if env_project:
                base = Path(env_project).resolve()
            elif self.default_project_path:
                base = Path(self.default_project_path).resolve()
            else:
                base = self.root_dir.resolve()

        slug = project_slug or project_slug_for_path(base)
        workspace_root = (self.projects_root_dir or self.root_dir / "projects") / slug
        data_root = workspace_root / "data"
        return ProjectConfig(
            project_slug=slug,
            project_name=base.name or slug,
            project_path=base,
            workspace_root=workspace_root,
            data_root=data_root,
            db_path=data_root / "db" / "obsmcp.sqlite3",
            json_export_dir=data_root / "json",
            backup_dir=data_root / "backups",
            export_dir=data_root / "exports",
            log_dir=workspace_root / "logs",
            vault_path=workspace_root / "vault",
            context_path=workspace_root / ".context",
            sessions_path=workspace_root / "sessions",
            manifest_path=workspace_root / "project.json",
            bridge_file_path=base / self.repo_bridge_filename,
        )


def _resolve(root_dir: Path, value: str) -> Path:
    path = Path(value)
    if path.is_absolute():
        return path
    return (root_dir / path).resolve()


def _parse_output_compression(payload: dict[str, object] | None) -> OutputCompressionConfig:
    if not isinstance(payload, dict):
        return OutputCompressionConfig()

    enabled = bool(payload.get("enabled", False))
    raw_mode = str(payload.get("mode", "") or "").strip().lower()
    mode = raw_mode or ("prompt_only" if enabled else "off")
    if not enabled:
        mode = "off"

    prompt_only_payload = payload.get("prompt_only", {})
    if not isinstance(prompt_only_payload, dict):
        prompt_only_payload = {}

    gateway_payload = payload.get("gateway_enforced", {})
    if not isinstance(gateway_payload, dict):
        gateway_payload = {}

    safety_payload = payload.get("safety_bypass", {})
    if not isinstance(safety_payload, dict):
        safety_payload = {}

    observability_payload = payload.get("observability", {})
    if not isinstance(observability_payload, dict):
        observability_payload = {}

    preserve_patterns = _default_preserve_patterns()
    raw_preserve_patterns = payload.get("preserve_patterns", {})
    if isinstance(raw_preserve_patterns, dict):
        preserve_patterns.update({str(key): bool(value) for key, value in raw_preserve_patterns.items()})

    task_overrides: dict[str, TaskOutputOverrideConfig] = {}
    raw_task_overrides = payload.get("task_overrides", {})
    if isinstance(raw_task_overrides, dict):
        for key, value in raw_task_overrides.items():
            if not isinstance(key, str) or not isinstance(value, dict):
                continue
            task_overrides[key] = TaskOutputOverrideConfig(
                mode=str(value.get("mode")).strip().lower() if value.get("mode") is not None else None,
                style=str(value.get("style")).strip() if value.get("style") is not None else None,
                level=str(value.get("level")).strip().lower() if value.get("level") is not None else None,
            )

    return OutputCompressionConfig(
        enabled=enabled,
        mode=mode,
        level=str(payload.get("level", "full")).strip().lower(),
        style=str(payload.get("style", "concise_professional")).strip(),
        respect_user_detail_requests=bool(payload.get("respect_user_detail_requests", True)),
        expand_on_request=bool(payload.get("expand_on_request", True)),
        task_overrides=task_overrides,
        preserve_patterns=preserve_patterns,
        prompt_only=PromptOnlyConfig(**prompt_only_payload) if prompt_only_payload else PromptOnlyConfig(),
        gateway_enforced=GatewayEnforcedConfig(**gateway_payload) if gateway_payload else GatewayEnforcedConfig(),
        safety_bypass=SafetyBypassConfig(**safety_payload) if safety_payload else SafetyBypassConfig(),
        observability=OutputObservabilityConfig(**observability_payload) if observability_payload else OutputObservabilityConfig(),
    )


def load_config(config_path: str | None = None) -> AppConfig:
    root_dir = Path(__file__).resolve().parent.parent
    config_file = Path(config_path).resolve() if config_path else root_dir / "config" / "obsmcp.json"
    payload = json.loads(config_file.read_text(encoding="utf-8"))

    # Determine default project path (env var overrides config file)
    env_default = os.environ.get("OBSMCP_PROJECT")
    if env_default:
        default_project = Path(env_default).resolve()
    elif payload.get("default_project_path"):
        default_project = _resolve(root_dir, payload["default_project_path"])
    else:
        default_project = root_dir.resolve()

    semantic_payload = payload.get("semantic", {})
    if not isinstance(semantic_payload, dict):
        semantic_payload = {}
    auto_generate_payload = semantic_payload.get("auto_generate", {})
    if not isinstance(auto_generate_payload, dict):
        auto_generate_payload = {}
    checkpoint_payload = payload.get("checkpoints", {})
    if not isinstance(checkpoint_payload, dict):
        checkpoint_payload = {}
    output_payload = payload.get("output_compression", {})
    if not isinstance(output_payload, dict):
        output_payload = {}

    config = AppConfig(
        root_dir=root_dir,
        app_name=payload["app_name"],
        description=payload["description"],
        host=os.getenv("OBSMCP_HOST", payload["host"]),
        port=int(os.getenv("OBSMCP_PORT", payload["port"])),
        bind_local_only=payload.get("bind_local_only", True),
        database_path=_resolve(root_dir, payload["database_path"]),
        json_export_dir=_resolve(root_dir, payload["json_export_dir"]),
        backup_dir=_resolve(root_dir, payload["backup_dir"]),
        log_dir=_resolve(root_dir, payload["log_dir"]),
        context_dir=_resolve(root_dir, payload["context_dir"]),
        obsidian_vault_dir=_resolve(root_dir, payload["obsidian_vault_dir"]),
        pid_file=_resolve(root_dir, payload["pid_file"]),
        max_recent_work_items=int(payload.get("max_recent_work_items", 12)),
        max_decisions=int(payload.get("max_decisions", 20)),
        max_blockers=int(payload.get("max_blockers", 20)),
        strict_project_routing=bool(payload.get("strict_project_routing", True)),
        bootstrap_default_project_on_startup=bool(payload.get("bootstrap_default_project_on_startup", False)),
        api_token=os.getenv("OBSMCP_API_TOKEN"),
        obsidian=ObsidianConfig(**payload["obsidian"]),
        logging=LoggingConfig(**payload["logging"]) if "logging" in payload else LoggingConfig(),
        default_project_path=default_project,
        workspace_root_dir=_resolve(root_dir, payload.get("workspace_root_dir", "./workspace")),
        projects_root_dir=_resolve(root_dir, payload.get("projects_root_dir", "./projects")),
        hub_vault_dir=_resolve(root_dir, payload.get("hub_vault_dir", "./hub/vault")),
        registry_path=_resolve(root_dir, payload.get("registry_path", "./registry/projects.json")),
        repo_bridge_filename=payload.get("repo_bridge_filename", ".obsmcp-link.json"),
        checkpoints=CheckpointConfig(**checkpoint_payload) if checkpoint_payload else CheckpointConfig(),
        semantic_auto_generate=SemanticAutoGenerateConfig(**auto_generate_payload) if auto_generate_payload else SemanticAutoGenerateConfig(),
        output_compression=_parse_output_compression(output_payload),
    )
    config.ensure_directories()
    return config
