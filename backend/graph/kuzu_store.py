"""Durable property-graph persistence on Kuzu.

This layer is deliberately dumb: it serialises nodes and edges, runs Cypher and
rehydrates results. All reasoning happens in :mod:`backend.graph.network_algorithms`
over a NetworkX projection -- Kuzu is the system of record and the ad-hoc query
surface, not the inference engine.
"""
from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import kuzu

from backend.core.models import (
    EdgeType,
    EpistemicStatus,
    KEdge,
    KNode,
    NodeType,
    Provenance,
    Quantity,
)
from backend.graph.schema import DDL

log = logging.getLogger("nexus.kuzu")


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


def _ts(dt: datetime | None) -> float | None:
    return dt.timestamp() if dt else None


def _parse_dt(s: Any) -> datetime | None:
    if not s:
        return None
    if isinstance(s, datetime):
        return s if s.tzinfo else s.replace(tzinfo=timezone.utc)
    try:
        dt = datetime.fromisoformat(str(s))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def node_params(n: KNode) -> dict[str, Any]:
    v = n.value or Quantity()
    return {
        "id": n.id,
        "type": n.type.value,
        "label": n.label,
        "body": n.body,
        "subject": n.subject,
        "predicate": n.predicate,
        "value_raw": v.raw or None,
        "value_number": v.number,
        "value_unit": v.unit,
        "value_bool": v.boolean,
        "value_text": v.text,
        "operator": n.operator,
        "status": n.status.value,
        "confidence": n.confidence,
        "criticality": n.criticality,
        "created_at": _iso(n.created_at),
        "updated_at": _iso(n.updated_at),
        "valid_from": _iso(n.valid_from),
        "valid_until": _iso(n.valid_until),
        "valid_from_ts": _ts(n.valid_from),
        "valid_until_ts": _ts(n.valid_until),
        "fingerprint": n.fingerprint,
        "tags": list(n.tags),
        "provenance_json": json.dumps([p.model_dump(mode="json") for p in n.provenance]),
        "attributes_json": json.dumps(n.attributes, default=str),
    }


def edge_params(e: KEdge) -> dict[str, Any]:
    return {
        "src": e.source,
        "dst": e.target,
        "id": e.id,
        "type": e.type.value,
        "weight": e.weight,
        "confidence": e.confidence,
        "created_at": _iso(e.created_at),
        "valid_from": _iso(e.valid_from),
        "valid_until": _iso(e.valid_until),
        "valid_from_ts": _ts(e.valid_from),
        "valid_until_ts": _ts(e.valid_until),
        "rationale": e.rationale,
        "provenance_json": json.dumps(
            e.provenance.model_dump(mode="json") if e.provenance else None, default=str
        ),
        "attributes_json": json.dumps(e.attributes, default=str),
    }


def row_to_node(r: dict[str, Any]) -> KNode:
    q = Quantity(
        raw=r.get("value_raw") or "",
        number=r.get("value_number"),
        unit=r.get("value_unit"),
        boolean=r.get("value_bool"),
        text=r.get("value_text"),
    )
    has_value = any((q.raw, q.number is not None, q.unit, q.boolean is not None, q.text))
    prov = [Provenance(**p) for p in json.loads(r.get("provenance_json") or "[]")]
    return KNode(
        id=r["id"],
        type=NodeType(r["type"]),
        label=r.get("label") or "",
        body=r.get("body") or "",
        subject=r.get("subject"),
        predicate=r.get("predicate"),
        value=q if has_value else None,
        operator=r.get("operator"),
        status=EpistemicStatus(r.get("status") or "UNVERIFIED"),
        confidence=r.get("confidence") if r.get("confidence") is not None else 0.5,
        created_at=_parse_dt(r.get("created_at")) or datetime.now(timezone.utc),
        updated_at=_parse_dt(r.get("updated_at")) or datetime.now(timezone.utc),
        valid_from=_parse_dt(r.get("valid_from")),
        valid_until=_parse_dt(r.get("valid_until")),
        fingerprint=r.get("fingerprint") or "",
        tags=list(r.get("tags") or []),
        provenance=prov,
        attributes=json.loads(r.get("attributes_json") or "{}"),
    )


def row_to_edge(r: dict[str, Any]) -> KEdge:
    pj = json.loads(r.get("provenance_json") or "null")
    return KEdge(
        id=r["id"],
        source=r["src"],
        target=r["dst"],
        type=EdgeType(r["type"]),
        weight=r.get("weight") if r.get("weight") is not None else 1.0,
        confidence=r.get("confidence") if r.get("confidence") is not None else 0.7,
        created_at=_parse_dt(r.get("created_at")) or datetime.now(timezone.utc),
        valid_from=_parse_dt(r.get("valid_from")),
        valid_until=_parse_dt(r.get("valid_until")),
        rationale=r.get("rationale") or "",
        provenance=Provenance(**pj) if pj else None,
        attributes=json.loads(r.get("attributes_json") or "{}"),
    )


_UPSERT_NODE = """
MERGE (n:KNode {id: $id})
SET n.type = $type, n.label = $label, n.body = $body,
    n.subject = $subject, n.predicate = $predicate,
    n.value_raw = $value_raw, n.value_number = $value_number,
    n.value_unit = $value_unit, n.value_bool = $value_bool,
    n.value_text = $value_text, n.operator = $operator,
    n.status = $status, n.confidence = $confidence, n.criticality = $criticality,
    n.created_at = $created_at, n.updated_at = $updated_at,
    n.valid_from = $valid_from, n.valid_until = $valid_until,
    n.valid_from_ts = $valid_from_ts, n.valid_until_ts = $valid_until_ts,
    n.fingerprint = $fingerprint, n.tags = $tags,
    n.provenance_json = $provenance_json, n.attributes_json = $attributes_json
"""

_UPSERT_EDGE = """
MATCH (a:KNode {id: $src}), (b:KNode {id: $dst})
MERGE (a)-[r:KEdge {id: $id}]->(b)
SET r.type = $type, r.weight = $weight, r.confidence = $confidence,
    r.created_at = $created_at, r.valid_from = $valid_from,
    r.valid_until = $valid_until, r.valid_from_ts = $valid_from_ts,
    r.valid_until_ts = $valid_until_ts, r.rationale = $rationale,
    r.provenance_json = $provenance_json, r.attributes_json = $attributes_json
"""

_ALL_NODES = "MATCH (n:KNode) RETURN n.*"
_ALL_EDGES = "MATCH (a:KNode)-[r:KEdge]->(b:KNode) RETURN a.id AS src, b.id AS dst, r.*"


class KuzuStore:
    """Thread-safe wrapper around a Kuzu database."""

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.path = path
        self.db = kuzu.Database(str(path))
        self.conn = kuzu.Connection(self.db)
        for stmt in DDL:
            self.conn.execute(stmt)

    # --- writes -----------------------------------------------------------
    def upsert_node(self, node: KNode) -> None:
        with self._lock:
            self.conn.execute(_UPSERT_NODE, node_params(node))

    def upsert_nodes(self, nodes: Iterable[KNode]) -> None:
        with self._lock:
            for n in nodes:
                self.conn.execute(_UPSERT_NODE, node_params(n))

    def upsert_edge(self, edge: KEdge) -> None:
        with self._lock:
            self.conn.execute(_UPSERT_EDGE, edge_params(edge))

    def upsert_edges(self, edges: Iterable[KEdge]) -> None:
        with self._lock:
            for e in edges:
                self.conn.execute(_UPSERT_EDGE, edge_params(e))

    def delete_node(self, node_id: str) -> None:
        with self._lock:
            self.conn.execute(
                "MATCH (a:KNode)-[r:KEdge]-(b:KNode) WHERE a.id = $id DELETE r",
                {"id": node_id},
            )
            self.conn.execute("MATCH (n:KNode) WHERE n.id = $id DELETE n", {"id": node_id})

    def delete_edge(self, edge_id: str) -> None:
        with self._lock:
            self.conn.execute(
                "MATCH ()-[r:KEdge]->() WHERE r.id = $id DELETE r", {"id": edge_id}
            )

    # --- reads ------------------------------------------------------------
    def query(self, cypher: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        """Run Cypher and return rows as dicts.

        Kuzu returns qualified column names ("n.label"). Stripping the alias
        makes rehydration convenient, but only where it stays unambiguous --
        a query selecting ``a.label`` and ``b.label`` keeps both qualified
        rather than silently dropping one.
        """
        with self._lock:
            res = self.conn.execute(cypher, params or {})
            raw_cols = list(res.get_column_names())
            stripped = [c.split(".", 1)[-1] if "." in c else c for c in raw_cols]
            cols = [
                s if stripped.count(s) == 1 else raw
                for raw, s in zip(raw_cols, stripped)
            ]
            out: list[dict[str, Any]] = []
            while res.has_next():
                out.append(dict(zip(cols, res.get_next())))
            return out

    def load_nodes(self) -> list[KNode]:
        return [row_to_node(r) for r in self.query(_ALL_NODES)]

    def load_edges(self) -> list[KEdge]:
        return [row_to_edge(r) for r in self.query(_ALL_EDGES)]

    def count(self) -> tuple[int, int]:
        n = self.query("MATCH (n:KNode) RETURN count(n) AS c")[0]["c"]
        e = self.query("MATCH ()-[r:KEdge]->() RETURN count(r) AS c")[0]["c"]
        return int(n), int(e)

    def close(self) -> None:
        """Release the connection *and* the database file lock.

        Closing only the connection leaves Kuzu holding the directory lock,
        which makes a reopen in the same process fail.
        """
        with self._lock:
            for obj in (getattr(self, "conn", None), getattr(self, "db", None)):
                try:
                    if obj is not None:
                        obj.close()
                except Exception:  # pragma: no cover - kuzu version differences
                    pass
