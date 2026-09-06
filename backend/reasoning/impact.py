"""Impact propagation.

When a claim moves, what else moves with it?

The spec defines impact as a sum over paths of the product of edge weights:

    Impact(v) = sum_p ( prod_{e in p} w_e )

Enumerating every path is exponential, so this implements the honest,
bounded version of that formula: a best-first (max-product) search that
accumulates the **top-K strongest distinct paths** into each node. Because the
frontier is popped in descending score order, the first path found to a node is
provably its strongest -- which is exactly the path we show the user as the
explanation. Weak paths are pruned below ``impact_min_score``; without pruning
a densely connected graph produces a long tail of 1e-6 scores that is noise.

Products of influences shrink with every hop, so depth attenuation is intrinsic
rather than a fudge factor bolted on afterwards.
"""
from __future__ import annotations

import heapq
import itertools
from datetime import datetime
from typing import Iterable, Sequence

import networkx as nx
from pydantic import BaseModel, Field

from backend.core.config import settings
from backend.core.models import NodeType
from backend.graph.network_algorithms import build_propagation_graph, criticality_scores
from backend.graph.store import KnowledgeStore


class ImpactHop(BaseModel):
    from_id: str
    to_id: str
    from_label: str
    to_label: str
    edge_type: str
    influence: float


class ImpactedNode(BaseModel):
    id: str
    label: str
    type: str
    status: str
    score: float               # accumulated propagation strength
    depth: int                 # hops along the strongest path
    criticality: float
    severity: float            # score * criticality -- "how much should I care"
    path: list[str]            # ids, seed -> this node
    explanation: str
    hops: list[ImpactHop] = Field(default_factory=list)


class ImpactReport(BaseModel):
    seeds: list[str]
    seed_labels: list[str] = Field(default_factory=list)
    computed_at: datetime
    affected: list[ImpactedNode] = Field(default_factory=list)
    affected_decisions: list[ImpactedNode] = Field(default_factory=list)
    max_depth_reached: int = 0
    total_affected: int = 0
    notes: list[str] = Field(default_factory=list)

    def top(self, n: int = 10) -> list[ImpactedNode]:
        return self.affected[:n]


def propagate(
    store: KnowledgeStore,
    seeds: Sequence[str] | dict[str, float],
    *,
    max_depth: int | None = None,
    min_score: float | None = None,
    paths_per_node: int = 3,
    when: datetime | None = None,
    graph: nx.DiGraph | None = None,
    exclude: Iterable[str] = (),
) -> ImpactReport:
    """Propagate a disturbance from ``seeds`` through the dependency graph.

    ``seeds`` may be a list of node ids (each gets magnitude 1.0) or a mapping
    of id -> magnitude, letting a 38% capacity drop hit harder than a 2% one.
    """
    max_depth = max_depth if max_depth is not None else settings.impact_max_depth
    min_score = min_score if min_score is not None else settings.impact_min_score
    seed_mag: dict[str, float] = (
        dict(seeds) if isinstance(seeds, dict) else {s: 1.0 for s in seeds}
    )
    excluded = set(exclude)

    # Impact runs over the graph *including* retired nodes. A requirement being
    # superseded is precisely why its dependents are disturbed, so filtering
    # dead nodes out here would sever the path at the very node that moved.
    # Status filtering belongs to belief queries, not to impact.
    G = (
        graph
        if graph is not None
        else build_propagation_graph(store, when=when, include_dead=True)
    )
    crit = criticality_scores(store, G)

    report = ImpactReport(
        seeds=list(seed_mag),
        seed_labels=[(store.get(s).label if store.get(s) else s) for s in seed_mag],
        computed_at=when or datetime.now().astimezone(),
    )

    live_seeds = [s for s in seed_mag if s in G]
    if not live_seeds:
        report.notes.append(
            "No seed is present in the live propagation graph "
            "(unknown id, or the node is superseded/expired)."
        )
        return report

    # --- bounded max-product best-first search ----------------------------
    counter = itertools.count()            # heap tiebreaker, keeps paths uncompared
    heap: list[tuple[float, int, int, str, tuple[str, ...]]] = []
    for s in live_seeds:
        heapq.heappush(heap, (-abs(seed_mag[s]), 0, next(counter), s, (s,)))

    pops: dict[str, int] = {}
    accumulated: dict[str, float] = {}
    best_path: dict[str, tuple[str, ...]] = {}
    seeds_set = set(live_seeds)

    while heap:
        neg, depth, _, node, path = heapq.heappop(heap)
        score = -neg
        if pops.get(node, 0) >= paths_per_node:
            continue
        pops[node] = pops.get(node, 0) + 1

        if node not in seeds_set and node not in excluded:
            accumulated[node] = accumulated.get(node, 0.0) + score
            if node not in best_path:          # first pop == strongest path
                best_path[node] = path
                report.max_depth_reached = max(report.max_depth_reached, depth)

        if depth >= max_depth:
            continue
        for succ in G.successors(node):
            if succ in path:                   # a path never revisits a node
                continue
            nxt = score * G[node][succ].get("influence", 0.0)
            if nxt < min_score:
                continue
            heapq.heappush(heap, (-nxt, depth + 1, next(counter), succ, path + (succ,)))

    # --- assemble -----------------------------------------------------------
    for nid, score in accumulated.items():
        node = store.get(nid)
        if node is None:
            continue
        path = list(best_path.get(nid, (nid,)))
        hops = _hops(store, G, path)
        c = crit.get(nid, node.criticality)
        report.affected.append(
            ImpactedNode(
                id=nid,
                label=node.label,
                type=node.type.value,
                status=node.status.value,
                score=round(min(score, 1.0), 4),
                depth=max(len(path) - 1, 0),
                criticality=round(c, 4),
                severity=round(min(score, 1.0) * c, 4),
                path=path,
                explanation=render_path(store, G, path),
                hops=hops,
            )
        )

    report.affected.sort(key=lambda a: (-a.severity, a.depth, a.id))
    report.affected_decisions = [
        a for a in report.affected if a.type == NodeType.DECISION.value
    ]
    report.total_affected = len(report.affected)
    if not report.affected:
        report.notes.append(
            "Nothing downstream: the seed has no outgoing dependencies above "
            f"the {min_score} propagation floor."
        )
    return report


def _hops(store: KnowledgeStore, G: nx.DiGraph, path: Sequence[str]) -> list[ImpactHop]:
    out: list[ImpactHop] = []
    for u, v in zip(path, path[1:]):
        data = G.get_edge_data(u, v) or {}
        nu, nv = store.get(u), store.get(v)
        out.append(
            ImpactHop(
                from_id=u,
                to_id=v,
                from_label=nu.label if nu else u,
                to_label=nv.label if nv else v,
                edge_type=data.get("type", "RELATED_TO"),
                influence=round(float(data.get("influence", 0.0)), 4),
            )
        )
    return out


def render_path(store: KnowledgeStore, G: nx.DiGraph, path: Sequence[str]) -> str:
    """A one-line, readable causal chain: 'X --INVALIDATES--> Y --BASED_ON--> Z'."""
    if len(path) < 2:
        node = store.get(path[0]) if path else None
        return node.label if node else "(seed)"
    parts: list[str] = []
    for i, (u, v) in enumerate(zip(path, path[1:])):
        data = G.get_edge_data(u, v) or {}
        nu, nv = store.get(u), store.get(v)
        if i == 0:
            parts.append(f"{nu.label if nu else u} [{nu.type.value if nu else '?'}]")
        parts.append(f"--{data.get('type', 'RELATED_TO')}-->")
        parts.append(f"{nv.label if nv else v} [{nv.type.value if nv else '?'}]")
    return " ".join(parts)


def explain_why_affected(
    store: KnowledgeStore, report: ImpactReport, node_id: str
) -> str:
    """Prose answer to 'why is this affected?', built from the graph path."""
    hit = next((a for a in report.affected if a.id == node_id), None)
    if hit is None:
        return f"{node_id} is not affected by this change."
    lines = [
        f"Why is {hit.label} ({hit.id}) affected?",
        "",
    ]
    for i, hop in enumerate(hit.hops, 1):
        lines.append(
            f"  {i}. {hop.from_label} --{hop.edge_type}--> {hop.to_label}"
            f"   (influence {hop.influence:.2f})"
        )
    lines += [
        "",
        f"Propagated strength {hit.score:.3f} over {hit.depth} hop(s); "
        f"node criticality {hit.criticality:.2f} gives severity {hit.severity:.3f}.",
    ]
    return "\n".join(lines)
