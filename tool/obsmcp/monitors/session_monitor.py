"""Starts a session on launch and heartbeats periodically."""

from __future__ import annotations

import asyncio
import platform

from ..client.http_client import BackendClient
from ..config import Config
from ..utils.logger import get_logger

logger = get_logger("obsmcp.session")


class SessionMonitor:
    def __init__(self, client: BackendClient, config: Config) -> None:
        self.client = client
        self.config = config
        self.heartbeat_interval = 60
        self.session: dict | None = None

    async def run(self) -> None:
        self.session = await self.client.start_session(
            self.config.project_id or "",
            context=f"OBSMCP started on {platform.node()}",
        )
        logger.info("Session started: %s", self.session["id"])
        try:
            while True:
                await asyncio.sleep(self.heartbeat_interval)
                await self.client.heartbeat_session(self.session["id"], context="")
        finally:
            if self.session:
                await self.client.end_session(self.session["id"])
                logger.info("Session ended: %s", self.session["id"])
