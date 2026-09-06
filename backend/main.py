"""NEXUS Omega application entry point.

Wires the layers together and owns their lifecycle: the knowledge graph is
rehydrated from Kuzu, the event bus is bound to the running loop (so the
watchdog thread can publish into it safely), the ingestion worker starts
draining source events, and the filesystem watcher begins reporting them.

Run with:  python -m backend.main
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from backend.api.routes import router
from backend.api.websocket import ws_router
from backend.core.config import settings
from backend.graph.store import get_store
from backend.ingestion.events import bus
from backend.ingestion.queue import worker
from backend.ingestion.watcher import SourceWatcher

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s %(name)-22s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("nexus")

watcher: SourceWatcher | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global watcher
    settings.ensure_dirs()

    bus.bind_loop()
    store = get_store()
    log.info("knowledge graph ready: %s", store.stats())

    from backend.semantic.embeddings import get_embedder
    from backend.semantic.llm import get_llm

    log.info("semantic compiler: %s | embedder: %s", get_llm().name, get_embedder().name)

    await worker.start()

    watcher = SourceWatcher()
    watcher.start()
    log.info("NEXUS Omega listening on http://%s:%d", settings.host, settings.port)

    try:
        yield
    finally:
        if watcher:
            watcher.stop()
        await worker.stop()
        store.close()
        log.info("shutdown complete")


app = FastAPI(
    title="NEXUS Omega",
    description=(
        "Autonomous semantic knowledge intelligence engine. Transforms "
        "heterogeneous sources into a living, temporal, explainable knowledge "
        "graph; detects change and contradiction; propagates impact; simulates "
        "alternatives."
    ),
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_origin_regex=r"http://(localhost|127\.0\.0\.1):\d+",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(router, prefix="/api")
app.include_router(ws_router)


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "nodes": len(get_store().nodes)}


def main() -> None:
    import uvicorn

    uvicorn.run(
        "backend.main:app",
        host=settings.host,
        port=settings.port,
        reload=False,
        log_level="info",
    )


if __name__ == "__main__":
    main()
