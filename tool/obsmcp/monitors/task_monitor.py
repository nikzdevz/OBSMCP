"""Optional: watches a tasks.json file in the project root for agent-friendly
sync between text-based task lists and OBSMCP's DB."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from ..client.http_client import BackendClient
from ..config import Config
from ..utils.logger import get_logger

logger = get_logger("obsmcp.tasks")


class TaskMonitor:
    def __init__(self, client: BackendClient, config: Config) -> None:
        self.client = client
        self.tasks_file = (
            Path(config.project_path) / "tasks.json" if config.project_path else None
        )
        self._last_mtime: float = 0.0

    async def run(self) -> None:
        if not self.tasks_file:
            return
        while True:
            await asyncio.sleep(10)
            try:
                if not self.tasks_file.exists():
                    continue
                mtime = self.tasks_file.stat().st_mtime
                if mtime == self._last_mtime:
                    continue
                self._last_mtime = mtime
                data = json.loads(self.tasks_file.read_text(encoding="utf-8"))
                if not isinstance(data, list):
                    continue
                for t in data:
                    if not isinstance(t, dict) or not t.get("title"):
                        continue
                    await self.client.create_task(t)
                logger.info("Imported %d tasks from %s", len(data), self.tasks_file)
            except Exception as exc:  # noqa: BLE001
                logger.warning("task monitor error: %s", exc)
