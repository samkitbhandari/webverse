"""The ingestion worker.

Source events arrive on the bus; this drains them one at a time and runs the
pipeline. Serialisation is deliberate. The pipeline reads the graph, decides
what a change means and writes back, and two documents doing that concurrently
would resolve entities against a graph that shifts underneath them -- producing
duplicate entities and missed contradictions. Ingest throughput is not the
bottleneck in this system; correctness of reconciliation is.

The pipeline itself is synchronous and CPU/IO-bound, so it runs in a worker
thread and the event loop stays free to serve the API and WebSocket clients.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from backend.core.models import CanonicalEvent, SemanticEventType, SourceEventType
from backend.ingestion.events import bus
from backend.pipeline import Pipeline, get_pipeline

log = logging.getLogger("nexus.queue")

SOURCE_EVENTS = {
    SourceEventType.SOURCE_CREATED,
    SourceEventType.SOURCE_MODIFIED,
    SourceEventType.SOURCE_DELETED,
    SourceEventType.SOURCE_MOVED,
}


class IngestionWorker:
    def __init__(self, pipeline: Pipeline | None = None):
        self.pipeline = pipeline or get_pipeline()
        self._task: asyncio.Task | None = None
        self._queue: asyncio.Queue[CanonicalEvent] | None = None
        self._stopping = False
        self.processed = 0
        self.failed = 0
        self.last_result: dict[str, Any] | None = None

    async def start(self) -> None:
        if self._task is not None:
            return
        self._stopping = False
        self._queue = bus.subscribe()
        self._task = asyncio.create_task(self._run(), name="nexus-ingest-worker")
        log.info("ingestion worker started")

    async def stop(self) -> None:
        self._stopping = True
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None
        if self._queue is not None:
            bus.unsubscribe(self._queue)
            self._queue = None
        log.info("ingestion worker stopped")

    async def _run(self) -> None:
        assert self._queue is not None
        while not self._stopping:
            try:
                event = await self._queue.get()
            except asyncio.CancelledError:
                raise
            if event.event_type not in SOURCE_EVENTS:
                continue
            path = event.payload.get("path") or event.source_id
            if not path:
                continue
            try:
                await self._process(event.event_type, path)
            except asyncio.CancelledError:
                raise
            except Exception:
                self.failed += 1
                log.exception("ingest failed for %s", path)
                bus.emit(
                    SemanticEventType.PIPELINE_STAGE,
                    {"stage": "error", "source": str(path),
                     "message": "ingestion failed; see server log"},
                    source_id=str(path),
                )

    async def _process(self, event_type: SourceEventType, path: str) -> None:
        result = await asyncio.to_thread(
            self.pipeline.handle_source_event, event_type, path
        )
        self.processed += 1
        if result is not None:
            self.last_result = {
                "source": result.source_label,
                "nodes_added": result.nodes_added,
                "edges_added": result.edges_added,
                "changes": len(result.material_changes),
                "contradictions": len(result.contradictions),
                "skipped": result.skipped_reason,
                "duration_ms": result.duration_ms,
            }

    def stats(self) -> dict[str, Any]:
        return {
            "processed": self.processed,
            "failed": self.failed,
            "running": self._task is not None and not self._task.done(),
            "last_result": self.last_result,
        }


worker = IngestionWorker()
