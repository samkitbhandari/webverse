"""NetworkX projections and graph algorithms.

Kuzu is the system of record; this module is where the graph becomes something
you can compute over. The central idea is the *propagation graph*: a DiGraph
whose edges point in the direction a change actually travels, which is not
always the direction the relationship is written in (see ``EDGE_DIRECTION``).
Every impact, criticality and centrality result is derived from it.
"""
from __future__ import annotations

from datetime import datetime
from typing import Iterable

import networkx as nx

from backend.core.models import (
    EDGE_DIRECTION,
    EdgeType,
    KEdge,
    KNode,
    NodeType,
    utcnow,
)
from backend.graph.store import KnowledgeStore


def _oriented(edge: KEdge) -> list[tuple[str, str]]:
    """The (u, v) pairs along which this edge transmits change."""
    d = EDGE_DIRECTION.get(edge.type, "forward")
    if d == "backward":
        return [(edge.target, edge.source)]
    if d == "both":
        return [(edge.source, edge.target), (edge.target, edge.source)]
    return [(edge.source, edge.target)]


def build_propagation_graph(
    store: KnowledgeStore,
    when: datetime | None = None,
    include_dead: bool = False,
) -> nx.DiGraph:
    """Directed graph where an edge u -> v means "a change in u disturbs v".

    Parallel relationships between the same pair are collapsed onto the
    strongest one, because impact should not be double-counted just because two
    documents asserted the same dependency.
    """
    t = when or utcnow()
    G = nx.DiGraph()

    for n in store.nodes.values():
        if not include_dead and not (n.status.is_live and n.is_valid_at(t)):
            continue
        G.add_node(
            n.id,
            type=n.type.value,
            label=n.label,
            status=n.status.value,
            confidence=n.confidence,
            criticality=n.criticality,
        )

    for e in store.edges.values():
        if not include_dead and not e.is_valid_at(t):
            continue
        infl = e.influence
        if infl <= 0:
            continue
        for u, v in _oriented(e):
            if u not in G or v not in G or u == v:
                continue
            prev = G.get_edge_data(u, v)
            if prev is None or infl > prev["influence"]:
                G.add_edge(
                    u, v,
                    influence=infl,
                    type=e.type.value,
                    edge_id=e.id,
                    rationale=e.rationale,
                    weight=e.weight,
                    confidence=e.confidence,
                )
    return G


def build_structural_graph(store: KnowledgeStore) -> nx.MultiDiGraph:
    """Literal graph, edges as written. Used for lineage and visualisation."""
    G = nx.MultiDiGraph()
    for n in store.nodes.values():
        G.add_node(n.id, **{"type": n.type.value, "label": n.label,
                            "status": n.status.value})
    for e in store.edges.values():
        G.add_edge(e.source, e.target, key=e.id, type=e.type.value,
                   weight=e.weight, confidence=e.confidence)
    return G


# ---------------------------------------------------------------------------
# Criticality
# ---------------------------------------------------------------------------
def criticality_scores(
    store: KnowledgeStore, G: nx.DiGraph | None = None
) -> dict[str, float]:
    """How much the graph as a whole leans on each node.

    Two ingredients: the intrinsic weight of the node's type (a Decision
    matters more than an Observation) and its structural position -- PageRank
    over the *reversed* propagation graph, which ranks a node by how much
    downstream knowledge would move if it moved.
    """
    G = G if G is not None else build_propagation_graph(store)
    if G.number_of_nodes() == 0:
        return {}

    try:
        pr = nx.pagerank(G.reverse(copy=True), alpha=0.85, weight="influence")
    except (nx.PowerIterationFailedConvergence, ZeroDivisionError):
        pr = {n: 1.0 / G.number_of_nodes() for n in G}

    hi = max(pr.values()) or 1.0
    out: dict[str, float] = {}
    for nid in G.nodes:
        node = store.get(nid)
        intrinsic = node.criticality if node else 0.5
        structural = pr.get(nid, 0.0) / hi
        out[nid] = round(0.55 * intrinsic + 0.45 * structural, 4)
    return out


def centrality_report(store: KnowledgeStore, top_k: int = 10) -> dict[str, list[dict]]:
    """Descriptive graph statistics for the knowledge-health dashboard."""
    G = build_propagation_graph(store)
    if G.number_of_nodes() == 0:
        return {"pagerank": [], "betweenness": [], "in_degree": []}

    R = G.reverse(copy=True)
    try:
        pr = nx.pagerank(R, alpha=0.85, weight="influence")
    except Exception:
        pr = {n: 0.0 for n in G}
    # betweenness is O(V*E); sample on anything sizeable
    k = min(G.number_of_nodes(), 64) if G.number_of_nodes() > 64 else None
    try:
        bc = nx.betweenness_centrality(G, k=k, weight=None, seed=7)
    except Exception:
        bc = {n: 0.0 for n in G}

    def _top(scores: dict[str, float]) -> list[dict]:
        ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)[:top_k]
        return [
            {
                "id": nid,
                "label": (store.get(nid).label if store.get(nid) else nid),
                "type": (store.get(nid).type.value if store.get(nid) else "?"),
                "score": round(float(s), 4),
            }
            for nid, s in ranked
        ]

    return {
        "pagerank": _top(pr),
        "betweenness": _top(bc),
        "in_degree": _top({n: float(G.out_degree(n)) for n in G}),
    }


# ---------------------------------------------------------------------------
# Traversal helpers
# ---------------------------------------------------------------------------
def downstream(
    store: KnowledgeStore, node_id: str, depth: int = 3, G: nx.DiGraph | None = None
) -> set[str]:
    """Every node reachable from a change at ``node_id`` within ``depth`` hops."""
    G = G if G is not None else build_propagation_graph(store)
    if node_id not in G:
        return set()
    return set(nx.single_source_shortest_path_length(G, node_id, cutoff=depth)) - {node_id}


def upstream(
    store: KnowledgeStore, node_id: str, depth: int = 3, G: nx.DiGraph | None = None
) -> set[str]:
    """Everything a node's current state rests on (its evidential support)."""
    G = G if G is not None else build_propagation_graph(store)
    if node_id not in G:
        return set()
    R = G.reverse(copy=True)
    return set(nx.single_source_shortest_path_length(R, node_id, cutoff=depth)) - {node_id}


def strongest_path(
    G: nx.DiGraph, source: str, target: str
) -> tuple[list[str], float] | None:
    """Max-product path -- the most plausible explanation of an influence.

    Products of probabilities become sums of negative logs, so the ordinary
    shortest-path machinery finds the *strongest* chain rather than the
    shortest one.
    """
    import math

    if source not in G or target not in G:
        return None
    H = nx.DiGraph()
    H.add_nodes_from(G.nodes)
    for u, v, data in G.edges(data=True):
        infl = max(min(data.get("influence", 0.0), 1.0), 1e-9)
        H.add_edge(u, v, cost=-math.log(infl))
    try:
        path = nx.shortest_path(H, source, target, weight="cost")
    except (nx.NetworkXNoPath, nx.NodeNotFound):
        return None
    score = 1.0
    for u, v in zip(path, path[1:]):
        score *= G[u][v].get("influence", 0.0)
    return path, score


def subgraph_around(
    store: KnowledgeStore, node_id: str, depth: int = 2, limit: int = 200
) -> tuple[list[KNode], list[KEdge]]:
    """Neighbourhood extraction for the graph UI."""
    seen: set[str] = {node_id}
    frontier = {node_id}
    for _ in range(depth):
        nxt: set[str] = set()
        for nid in frontier:
            for e in store.incident(nid):
                other = e.target if e.source == nid else e.source
                if other not in seen:
                    nxt.add(other)
            if len(seen) + len(nxt) >= limit:
                break
        seen |= nxt
        frontier = nxt
        if not frontier or len(seen) >= limit:
            break

    nodes = [store.nodes[n] for n in seen if n in store.nodes]
    edges = [
        e for e in store.edges.values() if e.source in seen and e.target in seen
    ]
    return nodes, edges


def to_cytoscape(nodes: Iterable[KNode], edges: Iterable[KEdge]) -> dict:
    """Serialise a subgraph in the shape the frontend force-graph expects."""
    return {
        "nodes": [
            {
                "id": n.id,
                "label": n.label,
                "type": n.type.value,
                "status": n.status.value,
                "confidence": round(n.confidence, 3),
                "criticality": round(n.criticality, 3),
                "subject": n.subject,
                "predicate": n.predicate,
                "value": n.value.render() if n.value else None,
                "valid_from": n.valid_from.isoformat() if n.valid_from else None,
                "valid_until": n.valid_until.isoformat() if n.valid_until else None,
            }
            for n in nodes
        ],
        "links": [
            {
                "id": e.id,
                "source": e.source,
                "target": e.target,
                "type": e.type.value,
                "weight": round(e.weight, 3),
                "confidence": round(e.confidence, 3),
                "rationale": e.rationale,
            }
            for e in edges
        ],
    }
