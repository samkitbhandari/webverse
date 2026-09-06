"""Active knowledge validation.

A passive system reports what it does not know. An active one works out *which*
unknown is worth the effort of resolving, and says so.

The measure used here is a value-of-information estimate. Total system
uncertainty is the criticality-weighted mass of doubt across live beliefs:

    U = sum_v (1 - confidence_v) * criticality_v

Resolving a claim ``c`` removes its own contribution and, in proportion to how
strongly ``c`` propagates to each downstream node, part of theirs:

    dU(c) = (1 - conf_c) * crit_c
          + sum_{v downstream} impact(c -> v) * (1 - conf_v) * crit_v

Ranking by ``dU`` produces answers of the form the spec asks for: "validate C37
first -- it influences 11 downstream decisions and would cut total uncertainty
by 32%."
"""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from backend.core.models import EpistemicStatus, KNode, NodeType, utcnow
from backend.graph.network_algorithms import build_propagation_graph, criticality_scores
from backend.graph.store import KnowledgeStore
from backend.reasoning.impact import propagate

#: States that represent genuine open doubt, as opposed to settled belief.
DOUBTFUL = (
    EpistemicStatus.UNVERIFIED,
    EpistemicStatus.UNCERTAIN,
    EpistemicStatus.CONTESTED,
    EpistemicStatus.PROBABLE,
    EpistemicStatus.CONTRADICTED,
)


class ValidationCandidate(BaseModel):
    id: str
    label: str
    type: str
    status: str
    confidence: float
    criticality: float
    downstream_nodes: int
    downstream_decisions: int
    uncertainty_reduction: float          # absolute, in units of U
    uncertainty_reduction_pct: float      # share of current total uncertainty
    reason: str
    suggested_action: str


class ValidationQueue(BaseModel):
    computed_at: datetime = Field(default_factory=utcnow)
    total_uncertainty: float = 0.0
    open_questions: int = 0
    candidates: list[ValidationCandidate] = Field(default_factory=list)
    summary: str = ""


def _uncertainty(node: KNode, crit: float) -> float:
    """Criticality-weighted doubt carried by one belief."""
    doubt = 1.0 - node.confidence
    if node.status is EpistemicStatus.CONTESTED:
        doubt = max(doubt, 0.5)           # a contested belief is doubtful even at high confidence
    elif node.status is EpistemicStatus.CONTRADICTED:
        doubt = max(doubt, 0.7)
    elif node.status is EpistemicStatus.UNVERIFIED:
        doubt = max(doubt, 0.4)
    return doubt * crit


def total_uncertainty(store: KnowledgeStore, crit: dict[str, float] | None = None) -> float:
    crit = crit if crit is not None else criticality_scores(store)
    return round(
        sum(
            _uncertainty(n, crit.get(n.id, n.criticality))
            for n in store.live_nodes()
        ),
        4,
    )


def build_queue(
    store: KnowledgeStore, *, limit: int = 10, max_depth: int = 4
) -> ValidationQueue:
    """Rank open uncertainties by how much resolving each would buy."""
    G = build_propagation_graph(store)
    crit = criticality_scores(store, G)
    U = total_uncertainty(store, crit)

    queue = ValidationQueue(total_uncertainty=U)
    doubtful = [
        n for n in store.live_nodes()
        if n.status in DOUBTFUL
        and n.type in (NodeType.CLAIM, NodeType.OBSERVATION, NodeType.ASSUMPTION,
                       NodeType.REQUIREMENT, NodeType.CONSTRAINT, NodeType.RISK)
    ]
    queue.open_questions = len(doubtful)

    for node in doubtful:
        own_crit = crit.get(node.id, node.criticality)
        own = _uncertainty(node, own_crit)

        report = propagate(store, [node.id], max_depth=max_depth, graph=G)
        downstream_gain = 0.0
        for a in report.affected:
            target = store.get(a.id)
            if target is None:
                continue
            downstream_gain += a.score * _uncertainty(
                target, crit.get(a.id, target.criticality)
            )

        gain = round(own + downstream_gain, 4)
        pct = round((gain / U * 100.0) if U > 0 else 0.0, 1)
        n_dec = len(report.affected_decisions)

        reason_bits = [f"confidence {node.confidence:.2f}", f"status {node.status.value}"]
        if report.total_affected:
            reason_bits.append(f"{report.total_affected} downstream node(s)")
        if n_dec:
            reason_bits.append(f"{n_dec} decision(s) depend on it")

        queue.candidates.append(
            ValidationCandidate(
                id=node.id,
                label=node.label,
                type=node.type.value,
                status=node.status.value,
                confidence=round(node.confidence, 3),
                criticality=round(own_crit, 3),
                downstream_nodes=report.total_affected,
                downstream_decisions=n_dec,
                uncertainty_reduction=gain,
                uncertainty_reduction_pct=pct,
                reason="; ".join(reason_bits),
                suggested_action=_suggest(node, report.total_affected, n_dec),
            )
        )

    queue.candidates.sort(key=lambda c: -c.uncertainty_reduction)
    queue.candidates = queue.candidates[:limit]

    if queue.candidates:
        top = queue.candidates[0]
        queue.summary = (
            f"{queue.open_questions} open uncertainties. Validate {top.id} "
            f"({top.label}) first - it influences {top.downstream_nodes} downstream "
            f"node(s) including {top.downstream_decisions} decision(s); expected "
            f"uncertainty reduction {top.uncertainty_reduction_pct:.0f}%."
        )
    else:
        queue.summary = "No open uncertainties: every live belief is settled."
    return queue


def _suggest(node: KNode, downstream: int, decisions: int) -> str:
    if node.status is EpistemicStatus.CONTESTED:
        return (
            "Two sources disagree and neither clearly outranks the other. "
            "Obtain a third, higher-authority source."
        )
    if node.status is EpistemicStatus.CONTRADICTED:
        return "Confirm whether the superseding value is correct, then retire this one."
    if node.status is EpistemicStatus.UNVERIFIED:
        return "No corroborating evidence yet. Attach a primary source."
    if decisions:
        return f"Re-confirm before the {decisions} dependent decision(s) are acted on."
    if downstream:
        return "Re-measure or re-confirm; several beliefs inherit this value."
    return "Low leverage - safe to defer."
