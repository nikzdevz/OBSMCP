"""Polls the project's git repo for branch/commit info."""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

from ..client.http_client import BackendClient
from ..config import Config
from ..utils.logger import get_logger

logger = get_logger("obsmcp.git")


class GitMonitor:
    def __init__(self, client: BackendClient, config: Config) -> None:
        self.client = client
        self.project_path = Path(config.project_path) if config.project_path else None
        self.interval = 60
        self._last_commit: str | None = None
        self._last_branch: str | None = None

    def _git(self, *args: str) -> str | None:
        if not self.project_path:
            return None
        try:
            result = subprocess.run(
                ["git", *args],
                cwd=self.project_path,
                check=True,
                capture_output=True,
                text=True,
                timeout=5,
            )
            return result.stdout.strip()
        except (subprocess.SubprocessError, FileNotFoundError):
            return None

    async def run(self) -> None:
        if not self.project_path or not (self.project_path / ".git").exists():
            logger.info("git monitor disabled (no .git in %s)", self.project_path)
            return
        while True:
            branch = self._git("rev-parse", "--abbrev-ref", "HEAD")
            commit = self._git("rev-parse", "HEAD")
            if branch and branch != self._last_branch:
                await self.client.log_work(
                    {
                        "description": f"Branch changed to {branch}",
                        "tags": ["git", "branch"],
                    }
                )
                self._last_branch = branch
            if commit and commit != self._last_commit:
                if self._last_commit is not None:
                    await self.client.log_work(
                        {
                            "description": f"New commit on {branch}: {commit[:8]}",
                            "tags": ["git", "commit"],
                        }
                    )
                self._last_commit = commit
            await asyncio.sleep(self.interval)
