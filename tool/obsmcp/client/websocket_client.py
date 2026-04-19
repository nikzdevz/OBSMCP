"""Optional WebSocket subscriber for server events (cloud mode only)."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from typing import Any

try:
    import websockets
except ImportError:  # pragma: no cover
    websockets = None  # type: ignore[assignment]

from ..utils.logger import get_logger

logger = get_logger("obsmcp.ws")

EventHandler = Callable[[dict[str, Any]], Awaitable[None]]


class DashboardWebSocket:
    def __init__(self, base_url: str, token: str) -> None:
        self.url = base_url.rstrip("/").replace("http://", "ws://").replace("https://", "wss://") + "/ws/dashboard"
        self.token = token
        self.on_event: EventHandler | None = None

    async def connect(self) -> None:
        if websockets is None:
            logger.warning("websockets package not installed; skipping WS client")
            return
        headers = [("Authorization", f"Bearer {self.token}")] if self.token else []
        while True:
            try:
                async with websockets.connect(self.url, extra_headers=headers) as ws:
                    async for message in ws:
                        try:
                            event = json.loads(message)
                        except json.JSONDecodeError:
                            continue
                        if self.on_event is not None:
                            await self.on_event(event)
            except Exception as exc:  # noqa: BLE001
                logger.warning("WS connect error: %s — retrying in 5s", exc)
                await asyncio.sleep(5)
