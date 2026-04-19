"""Watches the project directory for file changes (best-effort, optional)."""

from __future__ import annotations

import asyncio
from pathlib import Path

from ..client.http_client import BackendClient
from ..config import Config
from ..utils.logger import get_logger

logger = get_logger("obsmcp.files")

try:
    from watchfiles import awatch

    HAS_WATCHFILES = True
except ImportError:  # pragma: no cover
    HAS_WATCHFILES = False
    awatch = None  # type: ignore[assignment]


class FileWatcher:
    def __init__(self, client: BackendClient, config: Config) -> None:
        self.client = client
        self.project_path = Path(config.project_path) if config.project_path else None
        self.debounce_seconds = 30
        self._pending: dict[str, float] = {}

    async def run(self) -> None:
        if not HAS_WATCHFILES or awatch is None:
            logger.warning("watchfiles not installed — file watcher disabled")
            return
        if not self.project_path or not self.project_path.exists():
            logger.warning("project path %s not found — file watcher disabled", self.project_path)
            return
        logger.info("Watching %s for changes", self.project_path)
        async for changes in awatch(str(self.project_path), recursive=True):
            for change_type, path in changes:
                logger.debug("file %s: %s", change_type.name, path)
                await asyncio.sleep(0)  # yield; real debounce handled upstream
