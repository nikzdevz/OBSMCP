"""Configuration management for the local OBSMCP tool.

Config is stored at ``~/.obsmcp/config.json`` (``%USERPROFILE%\\.obsmcp\\config.json``
on Windows).
"""

from __future__ import annotations

import json
import os
import platform
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


def config_dir() -> Path:
    return Path(os.path.expanduser("~/.obsmcp"))


def config_path() -> Path:
    return config_dir() / "config.json"


def default_db_path() -> str:
    return str(config_dir() / "data" / "obsmcp.db")


@dataclass
class EnabledModules:
    task_monitor: bool = True
    session_monitor: bool = True
    file_watcher: bool = True
    git_monitor: bool = True
    perf_monitor: bool = True
    code_atlas: bool = True
    semantic_index: bool = False
    knowledge_graph: bool = True


@dataclass
class Config:
    version: str = "1.0.0"
    project_path: str = ""
    project_name: str = ""
    project_id: str = ""
    agent_id: str = field(
        default_factory=lambda: f"agent-{platform.system().lower()}-{uuid.uuid4().hex[:12]}"
    )
    mode: str = "standalone"  # "standalone" or "cloud"
    backend_url: str = ""
    api_token: str = ""
    enabled_modules: EnabledModules = field(default_factory=EnabledModules)
    scan_interval_seconds: int = 300
    perf_log_interval_seconds: int = 30
    graph_build_interval_seconds: int = 600
    llm_model: str = "claude-opus-4-5"
    llm_base_url: str = "https://api.anthropic.com/v1"
    llm_api_key_env: str = "ANTHROPIC_API_KEY"
    local_db_path: str = field(default_factory=default_db_path)
    local_ui_port: int = 8000

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Config:
        modules = EnabledModules(**(data.pop("enabled_modules", {}) or {}))
        return cls(enabled_modules=modules, **data)


def load_config() -> Config:
    """Load config from disk. Returns defaults if file missing."""
    path = config_path()
    if not path.exists():
        return Config()
    with path.open("r", encoding="utf-8") as f:
        return Config.from_dict(json.load(f))


def save_config(cfg: Config) -> None:
    config_dir().mkdir(parents=True, exist_ok=True)
    Path(cfg.local_db_path).parent.mkdir(parents=True, exist_ok=True)
    with config_path().open("w", encoding="utf-8") as f:
        json.dump(cfg.to_dict(), f, indent=2)


def configure(
    project_path: str,
    backend_url: str = "",
    api_token: str = "",
) -> Config:
    """Create or update config with supplied values and persist it."""
    cfg = load_config()
    if project_path:
        cfg.project_path = str(Path(project_path).expanduser().resolve())
        cfg.project_name = Path(cfg.project_path).name
        if not cfg.project_id:
            cfg.project_id = f"project-{uuid.uuid4().hex[:12]}"
    cfg.backend_url = backend_url.strip()
    cfg.api_token = api_token.strip()
    cfg.mode = "cloud" if cfg.backend_url else "standalone"
    save_config(cfg)
    return cfg
