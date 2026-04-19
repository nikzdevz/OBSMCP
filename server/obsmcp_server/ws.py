"""Dashboard WebSocket endpoint.

Mirrors the SSE stream so clients that prefer bidirectional transport
can subscribe to the same event bus.
"""

from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from .sse import register_listener, unregister_listener

router = APIRouter()


@router.websocket("/ws/dashboard")
async def dashboard_ws(ws: WebSocket) -> None:
    await ws.accept()
    queue = await register_listener()
    try:
        await ws.send_text(json.dumps({"type": "connected"}))
        while True:
            try:
                event = await asyncio.wait_for(queue.get(), timeout=15.0)
                await ws.send_text(json.dumps(event))
            except TimeoutError:
                await ws.send_text(json.dumps({"type": "heartbeat"}))
    except WebSocketDisconnect:
        pass
    finally:
        await unregister_listener(queue)
