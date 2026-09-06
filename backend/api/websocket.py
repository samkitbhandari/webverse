"""Live event stream.

Clients connect once and receive every canonical event as it happens, so the
graph animates instead of being polled. Two details keep it robust under a live
demo: a replay of recent history on connect (a client joining mid-ingest still
sees what just happened) and per-client backpressure handling, so one stalled
browser tab cannot slow the pipeline down.
"""
from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from backend.graph.store import get_store
from backend.ingestion.events import bus

log = logging.getLogger("nexus.ws")
ws_router = APIRouter()


@ws_router.websocket("/ws")
async def stream(websocket: WebSocket, replay: int = 25) -> None:
    await websocket.accept()
    queue = bus.subscribe()
    try:
        await websocket.send_json({
            "event_type": "CONNECTED",
            "payload": {
                "stats": get_store().stats(),
                "subscribers": bus.subscriber_count,
            },
        })
        for event in bus.recent(max(replay, 0)):
            await websocket.send_json({**event.ws(), "replayed": True})

        while True:
            event = await queue.get()
            await websocket.send_json(event.ws())
    except WebSocketDisconnect:
        pass
    except asyncio.CancelledError:
        raise
    except Exception:
        log.exception("websocket stream failed")
    finally:
        bus.unsubscribe(queue)
