"""Server-Sent Events broadcaster.

Every mutation in a router calls :func:`broadcast_event` which puts an event
into every connected listener's queue. The SSE generator yields it as a
``text/event-stream`` payload.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from datetime import UTC, datetime
from typing import Any

_listeners: list[asyncio.Queue[dict[str, Any]]] = []
_lock = asyncio.Lock()


async def register_listener() -> asyncio.Queue[dict[str, Any]]:
    q: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=1024)
    async with _lock:
        _listeners.append(q)
    return q


async def unregister_listener(q: asyncio.Queue[dict[str, Any]]) -> None:
    async with _lock:
        if q in _listeners:
            _listeners.remove(q)


def listener_count() -> int:
    return len(_listeners)


def broadcast_event(event_type: str, payload: dict[str, Any] | None = None) -> None:
    """Non-blocking broadcast to all connected SSE listeners.

    Safe to call from anywhere (sync request handlers). Uses
    ``call_soon_threadsafe`` when invoked from a different thread.
    """
    event = {
        "type": event_type,
        "payload": payload or {},
        "timestamp": datetime.now(UTC).isoformat(),
    }
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        # No loop in this thread; silently drop (not fatal).
        return

    for q in list(_listeners):
        try:
            loop.call_soon_threadsafe(_put_nowait, q, event)
        except RuntimeError:
            continue


def _put_nowait(q: asyncio.Queue[dict[str, Any]], event: dict[str, Any]) -> None:
    with contextlib.suppress(asyncio.QueueFull):
        q.put_nowait(event)


def format_sse(event: dict[str, Any]) -> str:
    return f"event: {event['type']}\ndata: {json.dumps(event)}\n\n"
