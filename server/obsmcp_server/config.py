"""Server configuration loaded from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _default_db_path() -> str:
    return str(Path.home() / ".obsmcp" / "data" / "obsmcp.db")


@dataclass(frozen=True)
class ServerConfig:
    api_token: str
    db_path: str
    host: str
    port: int
    mode: str  # "cloud" if api_token is set, else "local"

    @classmethod
    def from_env(cls) -> ServerConfig:
        token = os.environ.get("OBSMCP_API_TOKEN", "").strip()
        db_path = os.path.expanduser(
            os.path.expandvars(os.environ.get("OBSMCP_DB_PATH") or _default_db_path())
        )
        host = os.environ.get("OBSMCP_HOST", "0.0.0.0")
        port = int(os.environ.get("OBSMCP_PORT", "8000"))
        return cls(
            api_token=token,
            db_path=db_path,
            host=host,
            port=port,
            mode="cloud" if token else "local",
        )


_cached: ServerConfig | None = None


def get_config() -> ServerConfig:
    global _cached
    if _cached is None:
        _cached = ServerConfig.from_env()
    return _cached


def reset_config_cache() -> None:
    """For tests."""
    global _cached
    _cached = None
