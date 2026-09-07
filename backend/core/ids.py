"""Stable, human-readable identifiers.

Judges and engineers both read the graph. "C182" is legible in a screenshot and
in a Markdown wikilink in a way a UUID never is, so ids are short type-prefixed
sequences persisted across restarts. Content addressing still exists alongside
them: every node also carries a ``fingerprint`` so re-ingesting an unchanged
source is idempotent.
"""
from __future__ import annotations

import hashlib
import json
import re
import threading
from pathlib import Path

from backend.core.config import settings
from backend.core.models import NodeType

PREFIXES: dict[NodeType, str] = {
    NodeType.ENTITY: "EN",
    NodeType.CLAIM: "C",
    NodeType.DECISION: "D",
    NodeType.REQUIREMENT: "R",
    NodeType.CONSTRAINT: "CN",
    NodeType.ASSUMPTION: "AS",
    NodeType.OBSERVATION: "OB",
    NodeType.EVENT: "EV",
    NodeType.RISK: "RK",
    NodeType.ACTION: "AC",
    NodeType.EVIDENCE: "E",
    NodeType.CONCEPT: "CP",
}

_lock = threading.Lock()
_counters: dict[str, int] = {}
_loaded = False


def _state_path() -> Path:
    return settings.data_dir / "counters.json"


def _load() -> None:
    global _loaded
    if _loaded:
        return
    p = _state_path()
    if p.exists():
        try:
            _counters.update(json.loads(p.read_text("utf-8")))
        except (json.JSONDecodeError, OSError):
            pass
    _loaded = True


def _persist() -> None:
    p = _state_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(_counters), "utf-8")
    tmp.replace(p)


def next_id(prefix: str) -> str:
    """Allocate the next id for a prefix, e.g. next_id('C') -> 'C183'."""
    with _lock:
        _load()
        n = _counters.get(prefix, 0) + 1
        _counters[prefix] = n
        _persist()
        return f"{prefix}{n}"


def node_id(node_type: NodeType) -> str:
    return next_id(PREFIXES.get(node_type, "X"))


def edge_id() -> str:
    return next_id("REL")


def event_id() -> str:
    return next_id("EVT")


def reserve(prefix: str, value: int) -> None:
    """Raise a counter so externally-supplied ids are never re-issued."""
    with _lock:
        _load()
        if value > _counters.get(prefix, 0):
            _counters[prefix] = value
            _persist()


def observe_id(existing: str) -> None:
    """Teach the allocator about an id that arrived from outside (e.g. a seed)."""
    m = re.fullmatch(r"([A-Za-z]+)(\d+)", existing or "")
    if m:
        reserve(m.group(1), int(m.group(2)))


def fingerprint(*parts: object) -> str:
    """Content hash used to detect 'we have already seen exactly this'."""
    h = hashlib.sha1()
    for p in parts:
        h.update(str(p).strip().lower().encode("utf-8", "ignore"))
        h.update(b"\x1f")
    return h.hexdigest()[:16]


def slugify(text: str, max_len: int = 60) -> str:
    """Filesystem- and wikilink-safe form of a label."""
    s = re.sub(r"[^\w\s-]", "", text, flags=re.UNICODE).strip().lower()
    s = re.sub(r"[\s_-]+", "-", s)
    return s[:max_len].strip("-") or "untitled"
