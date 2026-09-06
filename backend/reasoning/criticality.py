"""Criticality ranking and knowledge health.

Two related questions, both answered by weighting the graph rather than
counting it: which nodes is the rest of the model leaning on, and how healthy
is the model overall. Health here is deliberately *not* "how much do we know" --
a large graph full of contested, unsourced, expired beliefs is less healthy
than a small graph of well-evidenced ones.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime

from pydantic import BaseModel, Field

from backend.core.models import EdgeType, EpistemicStatus, NodeType, utcnow
from backend.graph.network_algorithms import build_propagation_graph, criticality_scores
from backend.graph.store import KnowledgeStore
from backend.reasoning import temporal
from backend.reasoning.contradiction import detect_all
from backend.reasoning.validation import total_uncertainty


class CriticalNode(BaseModel):
    id: str
    label: str
    type: str
    status: str
    criticality: float
    dependents: int
    reason: str


class KnowledgeHealth(BaseModel):
    computed_at: datetime = Field(default_factory=utcnow)
    score: float = 0.0                      # 0..100 composite
    grade: str = ""
    nodes: int = 0
    edges: int = 0
    by_type: dict[str, int] = Field(default_factory=dict)
    by_status: dict[str, int] = Field(default_factory=dict)
    average_confidence: float = 0.0
    total_uncertainty: float = 0.0
    open_contradictions: int = 0
    contradiction_severity: float = 0.0
    contested: int = 0
    superseded: int = 0
    expired_beliefs: int = 0
    unsourced_claims: int = 0
    orphan_nodes: int = 0
    decisions_without_lineage: int = 0
    decisions_at_risk: int = 0
    issues: list[str] = Field(default_factory=list)


def rank_critical(store: KnowledgeStore, top_k: int = 10) -> list[CriticalNode]:
    """Nodes the rest of the graph most depends on."""
    G = build_propagation_graph(store)
    scores = criticality_scores(store, G)
    ranked = sorted(scores.items(), key=lambda kv: -kv[1])[:top_k]

    out: list[CriticalNode] = []
    for nid, score in ranked:
        node = store.get(nid)
        if node is None:
            continue
        dependents = G.out_degree(nid) if nid in G else 0
        out.append(
            CriticalNode(
                id=nid,
                label=node.label,
                type=node.type.value,
                status=node.status.value,
                criticality=round(score, 4),
                dependents=int(dependents),
                reason=(
                    f"{int(dependents)} belief(s) inherit from it; "
                    f"type weight {node.criticality:.2f}"
                ),
            )
        )
    return out


def knowledge_health(store: KnowledgeStore, when: datetime | None = None) -> KnowledgeHealth:
    """Dashboard metrics: is the model trustworthy right now?"""
    now = when or utcnow()
    h = KnowledgeHealth(computed_at=now, nodes=len(store.nodes), edges=len(store.edges))

    by_type: dict[str, int] = defaultdict(int)
    by_status: dict[str, int] = defaultdict(int)
    conf_sum, conf_n = 0.0, 0

    for n in store.nodes.values():
        by_type[n.type.value] += 1
        by_status[n.status.value] += 1
        if n.status.is_live:
            conf_sum += n.confidence
            conf_n += 1
    h.by_type = dict(by_type)
    h.by_status = dict(by_status)
    h.average_confidence = round(conf_sum / conf_n, 4) if conf_n else 0.0
    h.contested = by_status.get(EpistemicStatus.CONTESTED.value, 0)
    h.superseded = by_status.get(EpistemicStatus.SUPERSEDED.value, 0)

    contradictions = detect_all(store, now)
    h.open_contradictions = len(contradictions)
    h.contradiction_severity = round(sum(c.severity for c in contradictions), 3)

    h.expired_beliefs = len(temporal.expired(store, now))
    h.total_uncertainty = total_uncertainty(store)

    # a claim with no provenance and no supporting evidence is hearsay
    unsourced = 0
    for n in store.of_type(NodeType.CLAIM, NodeType.OBSERVATION, NodeType.ASSUMPTION):
        if not n.status.is_live:
            continue
        has_prov = any(p.source_path or p.source_id for p in n.provenance)
        has_support = any(e.type is EdgeType.SUPPORTS for e in store.in_edges(n.id))
        if not (has_prov or has_support):
            unsourced += 1
    h.unsourced_claims = unsourced

    h.orphan_nodes = sum(
        1 for n in store.nodes.values() if not store.incident(n.id)
    )

    decisions = store.of_type(NodeType.DECISION)
    no_lineage, at_risk = 0, 0
    for d in decisions:
        basis = temporal.decision_basis(store, d.id)
        if not basis:
            no_lineage += 1
            continue
        if any(n.status in temporal.MOVED for n, _ in basis):
            at_risk += 1
    h.decisions_without_lineage = no_lineage
    h.decisions_at_risk = at_risk

    h.score, h.grade, h.issues = _score(h, len(decisions))
    return h


def _score(h: KnowledgeHealth, n_decisions: int) -> tuple[float, str, list[str]]:
    """Composite 0-100 health score, with the reasons it is not 100."""
    issues: list[str] = []
    score = 100.0
    live = max(h.nodes - h.superseded, 1)

    penalty_conf = (1.0 - h.average_confidence) * 20.0
    score -= penalty_conf
    if h.average_confidence < 0.6:
        issues.append(f"Average confidence is only {h.average_confidence:.2f}.")

    if h.open_contradictions:
        p = min(h.contradiction_severity * 6.0, 25.0)
        score -= p
        issues.append(
            f"{h.open_contradictions} unresolved contradiction(s), "
            f"severity mass {h.contradiction_severity:.2f}."
        )

    if h.unsourced_claims:
        p = min(h.unsourced_claims / live * 40.0, 15.0)
        score -= p
        issues.append(f"{h.unsourced_claims} live claim(s) carry no evidence.")

    if h.expired_beliefs:
        p = min(h.expired_beliefs / live * 40.0, 12.0)
        score -= p
        issues.append(f"{h.expired_beliefs} belief(s) are past their validity window.")

    if h.decisions_at_risk:
        p = min(h.decisions_at_risk / max(n_decisions, 1) * 30.0, 20.0)
        score -= p
        issues.append(
            f"{h.decisions_at_risk} of {n_decisions} decision(s) rest on knowledge "
            "that has since moved."
        )

    if h.decisions_without_lineage:
        p = min(h.decisions_without_lineage / max(n_decisions, 1) * 20.0, 10.0)
        score -= p
        issues.append(
            f"{h.decisions_without_lineage} decision(s) have no recorded basis."
        )

    if h.orphan_nodes:
        p = min(h.orphan_nodes / live * 20.0, 8.0)
        score -= p

    score = round(max(score, 0.0), 1)
    grade = (
        "healthy" if score >= 80
        else "watch" if score >= 60
        else "degraded" if score >= 40
        else "critical"
    )
    if not issues:
        issues.append("No structural problems detected.")
    return score, grade, issues
