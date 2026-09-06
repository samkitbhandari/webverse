"""The canonical event bus.

Everything that happens in NEXUS -- a file landing, a claim changing, a
contradiction surfacing, an impact being computed -- is published here as a
:class:`CanonicalEvent`. The WebSocket layer, the vault writer and any future
subscriber all read from the same stream, which is what keeps the architecture
modular: no component calls the UI, they publish and the UI listens.

The bus is deliberately thread-aware. Watchdog delivers filesystem events on
its own observer thread, well outside the asyncio loop, so there is an explicit
thread-safe publish path rather than an accident waiting to happen.
"""
from __future__ import annotations

import asyncio
import logging
from collections import deque
from typing import Any, Iterable

from backend.core.ids import event_id
from backend.core.models import CanonicalEvent, SemanticEventType, SourceEventType

log = logging.getLogger("nexus.bus")


class EventBus:
    def __init__(self, history: int = 300, queue_size: int = 1000):
        self._subscribers: set[asyncio.Queue[CanonicalEvent]] = set()
        self._history: deque[CanonicalEvent] = deque(maxlen=history)
        self._loop: asyncio.AbstractEventLoop | None = None
        self._queue_size = queue_size

    # -- lifecycle ---------------------------------------------------------
    def bind_loop(self, loop: asyncio.AbstractEventLoop | None = None) -> None:
        """Remember the loop so other threads can publish into it."""
        self._loop = loop or asyncio.get_running_loop()

    # -- subscription ------------------------------------------------------
    def subscribe(self) -> asyncio.Queue[CanonicalEvent]:
        q: asyncio.Queue[CanonicalEvent] = asyncio.Queue(maxsize=self._queue_size)
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue[CanonicalEvent]) -> None:
        self._subscribers.discard(q)

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    # -- publishing --------------------------------------------------------
    def publish(self, event: CanonicalEvent) -> CanonicalEvent:
        """Publish from the event loop thread (or any thread, if bound)."""
        self._history.append(event)
        for q in list(self._subscribers):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                # A stalled client must never block the pipeline. Drop the
                # oldest event for that subscriber and keep going.
                try:
                    q.get_nowait()
                    q.put_nowait(event)
                except Exception:
                    log.warning("dropping event for a saturated subscriber")
        return event

    def publish_threadsafe(self, event: CanonicalEvent) -> CanonicalEvent:
        """Publish from a non-asyncio thread (the watchdog observer)."""
        if self._loop is None or self._loop.is_closed():
            self._history.append(event)
            return event
        self._loop.call_soon_threadsafe(self.publish, event)
        return event

    def emit(
        self,
        event_type: SourceEventType | SemanticEventType,
        payload: dict[str, Any] | None = None,
        source_id: str | None = None,
        threadsafe: bool = False,
    ) -> CanonicalEvent:
        ev = CanonicalEvent(
            event_id=event_id(),
            event_type=event_type,
            source_id=source_id,
            payload=payload or {},
        )
        return self.publish_threadsafe(ev) if threadsafe else self.publish(ev)

    # -- replay ------------------------------------------------------------
    def recent(self, limit: int = 50, types: Iterable[str] | None = None) -> list[CanonicalEvent]:
        """The last ``limit`` events, optionally filtered by type.

        ``limit <= 0`` means *none*. Guarding it explicitly matters because
        ``items[-0:]`` is ``items[0:]`` -- a client asking to skip replay would
        otherwise be handed the entire history buffer.
        """
        if limit <= 0:
            return []
        items = list(self._history)[-limit * 4 :]
        if types:
            wanted = set(types)
            items = [e for e in items if e.event_type.value in wanted]
        return items[-limit:]


bus = EventBus()
