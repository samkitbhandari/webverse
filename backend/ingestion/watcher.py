"""Filesystem watcher.

Watches ``incoming_files/`` and turns raw OS events into canonical source
events. Two details matter more than they look:

**Debouncing.** A single save from an editor produces a burst of MODIFIED
events, and a large file copy produces one per buffer flush. Ingesting each of
them means extracting the same document five times, or worse, extracting a
half-written file. Events are therefore coalesced per path over a short window.

**Stability checking.** Before a file is handed downstream its size is sampled
twice; a file still growing is not ready to parse. This is the cheap version of
the problem every ingestion pipeline eventually hits.
"""
from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Callable

from watchdog.events import (
    FileSystemEvent,
    FileSystemEventHandler,
)
from watchdog.observers import Observer

from backend.core.config import settings
from backend.core.models import SourceEventType
from backend.ingestion.events import bus
from backend.parsing.docling_parser import is_supported

log = logging.getLogger("nexus.watcher")

IGNORE_PREFIXES = (".", "~$", "__")
IGNORE_SUFFIXES = (".tmp", ".swp", ".part", ".crdownload", ".lock")


def _ignored(path: Path) -> bool:
    name = path.name
    return (
        name.startswith(IGNORE_PREFIXES)
        or name.endswith(IGNORE_SUFFIXES)
        or not is_supported(path)
    )


class _Debouncer:
    """Collapse a burst of events per path into one, after a quiet period."""

    def __init__(self, delay: float, on_fire: Callable[[str, SourceEventType, dict], None]):
        self.delay = delay
        self.on_fire = on_fire
        self._pending: dict[str, tuple[SourceEventType, dict, float]] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True, name="nexus-debounce")

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def push(self, path: str, kind: SourceEventType, extra: dict | None = None) -> None:
        with self._lock:
            prev = self._pending.get(path)
            # CREATED followed by MODIFIED is still a creation
            if prev and prev[0] is SourceEventType.SOURCE_CREATED:
                kind = SourceEventType.SOURCE_CREATED
            self._pending[path] = (kind, extra or {}, time.monotonic())

    def _run(self) -> None:
        while not self._stop.is_set():
            time.sleep(0.2)
            now = time.monotonic()
            ready: list[tuple[str, SourceEventType, dict]] = []
            with self._lock:
                for path, (kind, extra, stamp) in list(self._pending.items()):
                    if now - stamp >= self.delay:
                        del self._pending[path]
                        ready.append((path, kind, extra))
            for path, kind, extra in ready:
                if kind is not SourceEventType.SOURCE_DELETED and not _stable(Path(path)):
                    self.push(path, kind, extra)      # still being written
                    continue
                try:
                    self.on_fire(path, kind, extra)
                except Exception:
                    log.exception("debounced handler failed for %s", path)


def _stable(path: Path, settle: float = 0.25) -> bool:
    """Has the file stopped growing?"""
    try:
        first = path.stat().st_size
        time.sleep(settle)
        return path.exists() and path.stat().st_size == first
    except OSError:
        return False


class _Handler(FileSystemEventHandler):
    def __init__(self, debouncer: _Debouncer):
        self.debouncer = debouncer

    def _push(self, path_str: str, kind: SourceEventType, extra: dict | None = None) -> None:
        path = Path(path_str)
        if _ignored(path):
            return
        self.debouncer.push(str(path), kind, extra)

    def on_created(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self._push(str(event.src_path), SourceEventType.SOURCE_CREATED)

    def on_modified(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self._push(str(event.src_path), SourceEventType.SOURCE_MODIFIED)

    def on_deleted(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            path = Path(str(event.src_path))
            if not _ignored(path):
                self.debouncer.push(str(path), SourceEventType.SOURCE_DELETED)

    def on_moved(self, event: FileSystemEvent) -> None:
        if event.is_directory:
            return
        src, dst = Path(str(event.src_path)), Path(str(event.dest_path))
        if not _ignored(src):
            self.debouncer.push(str(src), SourceEventType.SOURCE_DELETED)
        if not _ignored(dst):
            self.debouncer.push(str(dst), SourceEventType.SOURCE_CREATED,
                                {"moved_from": str(src)})


class SourceWatcher:
    """Public handle: start(), stop(), and a scan() for what is already there."""

    def __init__(self, directory: Path | None = None, delay: float | None = None):
        self.directory = Path(directory or settings.incoming_dir)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.debouncer = _Debouncer(
            delay if delay is not None else settings.debounce_seconds, self._fire
        )
        self.observer = Observer()
        self._started = False

    def _fire(self, path: str, kind: SourceEventType, extra: dict) -> None:
        payload = {"path": path, "name": Path(path).name, **extra}
        try:
            if kind is not SourceEventType.SOURCE_DELETED:
                payload["size"] = Path(path).stat().st_size
        except OSError:
            pass
        log.info("%s %s", kind.value, Path(path).name)
        bus.emit(kind, payload, source_id=path, threadsafe=True)

    def start(self) -> None:
        if self._started:
            return
        self.debouncer.start()
        self.observer.schedule(_Handler(self.debouncer), str(self.directory), recursive=True)
        self.observer.start()
        self._started = True
        log.info("watching %s", self.directory)

    def stop(self) -> None:
        if not self._started:
            return
        self.debouncer.stop()
        self.observer.stop()
        self.observer.join(timeout=3)
        self._started = False

    def scan(self) -> list[Path]:
        """Files already present at startup, so a restart is not amnesia."""
        return [
            p for p in sorted(self.directory.rglob("*"))
            if p.is_file() and not _ignored(p)
        ]

    def replay_existing(self) -> int:
        """Emit CREATED for everything already in the directory."""
        found = self.scan()
        for p in found:
            self._fire(str(p), SourceEventType.SOURCE_CREATED, {"replayed": True})
        return len(found)
