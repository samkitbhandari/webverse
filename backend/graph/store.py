"""The living knowledge graph.

An in-memory working set (fast, indexed, what the reasoning engines traverse)
with write-through persistence to Kuzu (durable, queryable). On boot the
working set is rehydrated from Kuzu, so the graph survives restarts.

The indices here exist for specific downstream questions:

* ``by_triple``      -- "what else asserts a value for (subject, predicate)?"
                        This is what makes contradiction detection O(1) rather
                        than a full scan on every ingest.
* ``by_fingerprint`` -- "have we already ingested exactly this?" (idempotency)
* ``entity_keys``    -- "does an entity by this name already exist?" (resolution)
"""
from __future__ import annotations

import logging
import threading
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Iterable, Iterator

from backend.core.config import settings
from backend.core.ids import observe_id
from backend.core.models import EdgeType, EpistemicStatus, KEdge, KNode, NodeType, utcnow
from backend.graph.kuzu_store import KuzuStore

log = logging.getLogger("nexus.store")


def entity_key(name: str) -> str:
    """Normalised lookup key for an entity name."""
    return " ".join(name.strip().lower().split())


class KnowledgeStore:
    def __init__(self, kuzu_path: Path | None = None, persist: bool = True):
        self._lock = threading.RLock()
        self.nodes: dict[str, KNode] = {}
        self.edges: dict[str, KEdge] = {}
        self.out_adj: dict[str, set[str]] = defaultdict(set)
        self.in_adj: dict[str, set[str]] = defaultdict(set)
        self.by_type: dict[NodeType, set[str]] = defaultdict(set)
        self.by_triple: dict[tuple[str, str], set[str]] = defaultdict(set)
        self.by_fingerprint: dict[str, str] = {}
        self.entity_keys: dict[str, str] = {}
        self.revision: int = 0

        self.kuzu: KuzuStore | None = None
        if persist:
            self.kuzu = KuzuStore(kuzu_path or settings.kuzu_dir)
            self.reload()

    # -- lifecycle ---------------------------------------------------------
    def reload(self) -> None:
        """Rehydrate the working set from durable storage."""
        if not self.kuzu:
            return
        with self._lock:
            self._reset_indices()
            for n in self.kuzu.load_nodes():
                self._index_node(n)
                observe_id(n.id)
            for e in self.kuzu.load_edges():
                self._index_edge(e)
                observe_id(e.id)
            log.info("rehydrated %d nodes / %d edges", len(self.nodes), len(self.edges))

    def _reset_indices(self) -> None:
        self.nodes.clear()
        self.edges.clear()
        self.out_adj.clear()
        self.in_adj.clear()
        self.by_type.clear()
        self.by_triple.clear()
        self.by_fingerprint.clear()
        self.entity_keys.clear()

    # -- indexing ----------------------------------------------------------
    def _index_node(self, n: KNode) -> None:
        self.nodes[n.id] = n
        self.by_type[n.type].add(n.id)
        if t := n.triple():
            self.by_triple[t].add(n.id)
        if n.fingerprint:
            self.by_fingerprint[n.fingerprint] = n.id
        if n.type is NodeType.ENTITY:
            self.entity_keys[entity_key(n.label)] = n.id
            for alias in n.attributes.get("aliases", []) or []:
                self.entity_keys.setdefault(entity_key(str(alias)), n.id)

    def _deindex_node(self, n: KNode) -> None:
        self.by_type[n.type].discard(n.id)
        if t := n.triple():
            self.by_triple[t].discard(n.id)
        if n.fingerprint and self.by_fingerprint.get(n.fingerprint) == n.id:
            del self.by_fingerprint[n.fingerprint]
        if n.type is NodeType.ENTITY:
            for k, v in list(self.entity_keys.items()):
                if v == n.id:
                    del self.entity_keys[k]

    def _index_edge(self, e: KEdge) -> None:
        self.edges[e.id] = e
        self.out_adj[e.source].add(e.id)
        self.in_adj[e.target].add(e.id)

    # -- writes ------------------------------------------------------------
    def add_node(self, node: KNode) -> KNode:
        with self._lock:
            if old := self.nodes.get(node.id):
                self._deindex_node(old)
            self._index_node(node)
            self.revision += 1
            if self.kuzu:
                self.kuzu.upsert_node(node)
            return node

    def update_node(self, node_id: str, **fields) -> KNode | None:
        """Mutate a node in place and write it through. Returns the new node."""
        with self._lock:
            cur = self.nodes.get(node_id)
            if not cur:
                return None
            self._deindex_node(cur)
            data = cur.model_dump()
            data.update(fields)
            data["updated_at"] = utcnow()
            new = KNode(**data)
            self._index_node(new)
            self.revision += 1
            if self.kuzu:
                self.kuzu.upsert_node(new)
            return new

    def add_edge(self, edge: KEdge) -> KEdge:
        with self._lock:
            self._index_edge(edge)
            self.revision += 1
            if self.kuzu:
                self.kuzu.upsert_edge(edge)
            return edge

    def add_edges(self, edges: Iterable[KEdge]) -> None:
        for e in edges:
            self.add_edge(e)

    def remove_node(self, node_id: str) -> None:
        with self._lock:
            n = self.nodes.pop(node_id, None)
            if not n:
                return
            self._deindex_node(n)
            for eid in list(self.out_adj[node_id] | self.in_adj[node_id]):
                self.remove_edge(eid)
            self.out_adj.pop(node_id, None)
            self.in_adj.pop(node_id, None)
            self.revision += 1
            if self.kuzu:
                self.kuzu.delete_node(node_id)

    def remove_edge(self, edge_id: str) -> None:
        with self._lock:
            e = self.edges.pop(edge_id, None)
            if not e:
                return
            self.out_adj[e.source].discard(edge_id)
            self.in_adj[e.target].discard(edge_id)
            self.revision += 1
            if self.kuzu:
                self.kuzu.delete_edge(edge_id)

    # -- reads -------------------------------------------------------------
    def get(self, node_id: str) -> KNode | None:
        return self.nodes.get(node_id)

    def of_type(self, *types: NodeType) -> list[KNode]:
        ids: set[str] = set()
        for t in types:
            ids |= self.by_type.get(t, set())
        return [self.nodes[i] for i in ids if i in self.nodes]

    def out_edges(self, node_id: str) -> list[KEdge]:
        return [self.edges[e] for e in self.out_adj.get(node_id, ()) if e in self.edges]

    def in_edges(self, node_id: str) -> list[KEdge]:
        return [self.edges[e] for e in self.in_adj.get(node_id, ()) if e in self.edges]

    def incident(self, node_id: str) -> list[KEdge]:
        return self.out_edges(node_id) + self.in_edges(node_id)

    def find_edge(self, source: str, target: str, type_: EdgeType) -> KEdge | None:
        for e in self.out_edges(source):
            if e.target == target and e.type is type_:
                return e
        return None

    def siblings_of_triple(self, subject: str, predicate: str) -> list[KNode]:
        """Every node asserting a value for the same (subject, predicate)."""
        key = (subject.strip().lower(), predicate.strip().lower())
        return [self.nodes[i] for i in self.by_triple.get(key, set()) if i in self.nodes]

    def live_nodes(self, when: datetime | None = None) -> Iterator[KNode]:
        """Nodes whose belief is still active and temporally valid."""
        t = when or utcnow()
        for n in self.nodes.values():
            if n.status.is_live and n.is_valid_at(t):
                yield n

    def find_entity(self, name: str) -> KNode | None:
        nid = self.entity_keys.get(entity_key(name))
        return self.nodes.get(nid) if nid else None

    def by_fp(self, fp: str) -> KNode | None:
        nid = self.by_fingerprint.get(fp)
        return self.nodes.get(nid) if nid else None

    def nodes_from_source(self, source_path: str) -> list[KNode]:
        """Everything a given file is responsible for having created."""
        out = []
        for n in self.nodes.values():
            if any(p.source_path == source_path for p in n.provenance):
                out.append(n)
        return out

    def stats(self) -> dict[str, int | float]:
        by_status: dict[str, int] = defaultdict(int)
        for n in self.nodes.values():
            by_status[n.status.value] += 1
        return {
            "nodes": len(self.nodes),
            "edges": len(self.edges),
            "revision": self.revision,
            **{f"status_{k}": v for k, v in by_status.items()},
            **{f"type_{t.value}": len(ids) for t, ids in self.by_type.items() if ids},
        }

    def clone(self) -> "KnowledgeStore":
        """A detached, in-memory copy with no persistence.

        Counterfactual simulation mutates beliefs freely -- invalidating
        claims, rewriting values, cutting dependencies -- and none of that may
        reach the real graph. The clone has ``kuzu=None``, so every write is
        structurally incapable of touching the system of record.
        """
        with self._lock:
            copy = KnowledgeStore(persist=False)
            for n in self.nodes.values():
                copy._index_node(n.model_copy(deep=True))
            for e in self.edges.values():
                copy._index_edge(e.model_copy(deep=True))
            copy.revision = self.revision
            return copy

    def query(self, cypher: str, params: dict | None = None) -> list[dict]:
        if not self.kuzu:
            raise RuntimeError("Cypher requires the persistent Kuzu backend")
        return self.kuzu.query(cypher, params)

    def close(self) -> None:
        if self.kuzu:
            self.kuzu.close()


# Process-wide singleton, created lazily so tests can build isolated stores.
_store: KnowledgeStore | None = None


def get_store() -> KnowledgeStore:
    global _store
    if _store is None:
        settings.ensure_dirs()
        _store = KnowledgeStore()
    return _store


def set_store(store: KnowledgeStore | None) -> None:
    """Install (or clear, with None) the process-wide store."""
    global _store
    _store = store
