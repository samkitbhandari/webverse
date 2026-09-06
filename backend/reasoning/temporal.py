"""Temporal reasoning over the knowledge graph.

Knowledge is bitemporal here, and the distinction matters:

* **valid time**       -- when the claim is true in the world
                          (``valid_from`` / ``valid_until``)
* **transaction time** -- when the system came to believe it, and when it
                          stopped (``created_at`` / ``attributes.retired_at``)

Keeping both is what lets NEXUS answer "what did we believe *at the time* that
decision was made?" rather than only "what do we believe now" -- which is the
difference between auditing a decision and second-guessing it with hindsight.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Iterable

from pydantic import BaseModel, Field

from backend.core.ids import edge_id
from backend.core.models import (
    EdgeType,
    EpistemicStatus,
    KEdge,
    KNode,
    NodeType,
    utcnow,
)
from backend.graph.store import KnowledgeStore

RETIRED_AT = "retired_at"          # attributes key: transaction-time end


class TimelinePoint(BaseModel):
    node_id: str
    label: str
    value: str | None
    status: str
    confidence: float
    valid_from: datetime | None
    valid_until: datetime | None
    created_at: datetime
    source: str | None = None


class Timeline(BaseModel):
    subject: str
    predicate: str
    points: list[TimelinePoint] = Field(default_factory=list)


def _aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Supersession
# ---------------------------------------------------------------------------
def supersede(
    store: KnowledgeStore,
    new_node: KNode,
    old_node: KNode,
    *,
    reason: str = "",
    when: datetime | None = None,
) -> KEdge:
    """Retire ``old_node`` in favour of ``new_node``.

    Three things happen, and all three are needed for the history to stay
    readable: the old belief's valid interval is closed at the moment the new
    one begins, its epistemic status becomes SUPERSEDED, and a SUPERSEDES edge
    records the succession so the timeline can be walked later.
    """
    now = when or utcnow()
    boundary = _aware(new_node.valid_from) or now

    updates: dict = {
        "status": EpistemicStatus.SUPERSEDED,
        "attributes": {**old_node.attributes, RETIRED_AT: now.isoformat(),
                       "superseded_by": new_node.id},
    }
    old_until = _aware(old_node.valid_until)
    if old_until is None or old_until > boundary:
        updates["valid_until"] = boundary
    store.update_node(old_node.id, **updates)

    edge = KEdge(
        id=edge_id(),
        source=new_node.id,
        target=old_node.id,
        type=EdgeType.SUPERSEDES,
        weight=0.95,
        confidence=max(new_node.confidence, 0.6),
        created_at=now,
        valid_from=boundary,
        rationale=reason or f"{new_node.label} replaces {old_node.label}",
    )
    return store.add_edge(edge)


def invalidate(
    store: KnowledgeStore,
    node: KNode,
    *,
    cause: KNode | None = None,
    reason: str = "",
    when: datetime | None = None,
) -> None:
    """Mark a belief as no longer holding, optionally attributing the cause."""
    now = when or utcnow()
    store.update_node(
        node.id,
        status=EpistemicStatus.INVALIDATED,
        valid_until=_aware(node.valid_until) or now,
        attributes={**node.attributes, RETIRED_AT: now.isoformat(),
                    "invalidated_reason": reason},
    )
    if cause is not None:
        store.add_edge(
            KEdge(
                id=edge_id(),
                source=cause.id,
                target=node.id,
                type=EdgeType.INVALIDATES,
                weight=1.0,
                confidence=cause.confidence,
                created_at=now,
                rationale=reason or f"{cause.label} invalidates {node.label}",
            )
        )


def find_predecessors(store: KnowledgeStore, node: KNode) -> list[KNode]:
    """Live claims about the same property that this one should replace.

    A newer claim supersedes an older one when they assert the same
    (subject, predicate) and the newer one's validity starts later. If their
    windows genuinely overlap the two are in conflict instead, and that is the
    contradiction engine's business, not this one's.
    """
    triple = node.triple()
    if not triple or node.type not in (NodeType.CLAIM, NodeType.OBSERVATION,
                                       NodeType.ASSUMPTION, NodeType.REQUIREMENT,
                                       NodeType.CONSTRAINT):
        return []

    out: list[KNode] = []
    n_from = _aware(node.valid_from) or _aware(node.created_at)
    for other in store.siblings_of_triple(*triple):
        if other.id == node.id or other.type is not node.type:
            continue
        if not other.status.is_live:
            continue
        o_from = _aware(other.valid_from) or _aware(other.created_at)
        if n_from and o_from and o_from < n_from:
            out.append(other)
    return sorted(out, key=lambda n: _aware(n.valid_from) or _aware(n.created_at))


# ---------------------------------------------------------------------------
# Point-in-time reconstruction
# ---------------------------------------------------------------------------
def known_from(node: KNode) -> datetime | None:
    """When the system effectively held this belief from.

    Normally that is ``created_at`` (transaction time). But knowledge is often
    *backfilled*: a document ingested today can state a requirement that has
    been in force for a year, and treating that as unknown until today would
    make every historical query read "not yet known". Where a source asserts an
    earlier ``valid_from``, we honour it as the start of the belief.
    """
    created = _aware(node.created_at)
    vf = _aware(node.valid_from)
    if created and vf:
        return min(created, vf)
    return created or vf


def was_believed_at(node: KNode, when: datetime) -> bool:
    """Whether the system held this belief at ``when`` (bitemporal test)."""
    start = known_from(node)
    if start and start > when:
        return False                       # we had not learned it yet
    retired_raw = node.attributes.get(RETIRED_AT)
    if retired_raw:
        try:
            retired = _aware(datetime.fromisoformat(str(retired_raw)))
            if retired and retired <= when:
                return False               # we had already given it up
        except ValueError:
            pass
    return node.is_valid_at(when)


def as_of(store: KnowledgeStore, when: datetime) -> list[KNode]:
    """The full belief state at a past instant."""
    when = _aware(when) or utcnow()
    return [n for n in store.nodes.values() if was_believed_at(n, when)]


#: Statuses that mean a belief underpinning a decision has moved under it.
MOVED = (
    EpistemicStatus.CONTRADICTED,
    EpistemicStatus.INVALIDATED,
    EpistemicStatus.SUPERSEDED,
    EpistemicStatus.CONTESTED,
)


def decision_basis(store: KnowledgeStore, decision_id: str) -> list[tuple[KNode, str]]:
    """Everything a decision rests on, in both edge directions.

    A decision's basis is not only what it points at. "R3 CONSTRAINS D17" is
    written requirement-first, but the requirement is still part of what D17
    stands on -- and it is exactly the edge a regulation change travels down.
    Looking only at outgoing edges misses that entire class of exposure.
    """
    out: list[tuple[KNode, str]] = []
    seen: set[str] = set()
    for e in store.out_edges(decision_id):
        if e.type in (EdgeType.BASED_ON, EdgeType.DEPENDS_ON, EdgeType.REQUIRES):
            if (n := store.get(e.target)) and n.id not in seen:
                seen.add(n.id)
                out.append((n, e.type.value))
    for e in store.in_edges(decision_id):
        if e.type in (EdgeType.CONSTRAINS, EdgeType.REQUIRES, EdgeType.INVALIDATES):
            if (n := store.get(e.source)) and n.id not in seen:
                seen.add(n.id)
                out.append((n, e.type.value))
    return out


def beliefs_behind_decision(store: KnowledgeStore, decision_id: str) -> dict:
    """What supported a decision when it was taken, and what has changed since.

    This is the query that turns a knowledge graph into an audit trail:
    "Decision D17 rested on C18 and C42. C42 has since been contradicted -- so
    revalidate D17."
    """
    decision = store.get(decision_id)
    if decision is None:
        return {"error": f"unknown decision {decision_id}"}

    taken_at = _aware(decision.valid_from) or _aware(decision.created_at) or utcnow()
    basis: list[dict] = []
    for n, rel in decision_basis(store, decision_id):
        known_then = was_believed_at(n, taken_at)
        basis.append(
            {
                "id": n.id,
                "label": n.label,
                "type": n.type.value,
                "relationship": rel,
                "believed_at_decision_time": known_then,
                "status_then": "SUPPORTED" if known_then else "NOT YET KNOWN",
                "status_now": n.status.value,
                "value_now": n.value.render() if n.value else None,
                "changed": n.status in MOVED,
            }
        )

    changed = [b for b in basis if b["changed"]]
    return {
        "decision": {
            "id": decision.id,
            "label": decision.label,
            "status": decision.status.value,
            "taken_at": taken_at.isoformat(),
        },
        "basis": basis,
        "changed_since": changed,
        "still_valid": not changed,
        "summary": (
            f"{decision.label} rested on {len(basis)} belief(s); "
            f"{len(changed)} of them have moved since it was taken."
            if basis
            else f"{decision.label} has no recorded evidential basis - lineage is incomplete."
        ),
    }


def timeline(store: KnowledgeStore, subject: str, predicate: str) -> Timeline:
    """The value history of one property, oldest first."""
    nodes = store.siblings_of_triple(subject, predicate)
    nodes.sort(key=lambda n: _aware(n.valid_from) or _aware(n.created_at) or utcnow())
    return Timeline(
        subject=subject,
        predicate=predicate,
        points=[
            TimelinePoint(
                node_id=n.id,
                label=n.label,
                value=n.value.render() if n.value else None,
                status=n.status.value,
                confidence=n.confidence,
                valid_from=n.valid_from,
                valid_until=n.valid_until,
                created_at=n.created_at,
                source=next((p.source_label or p.source_path for p in n.provenance), None),
            )
            for n in nodes
        ],
    )


def node_timeline(store: KnowledgeStore, node_id: str) -> Timeline | None:
    node = store.get(node_id)
    if node is None:
        return None
    t = node.triple()
    if not t:
        return Timeline(
            subject=node.subject or node.label,
            predicate=node.predicate or "-",
            points=[
                TimelinePoint(
                    node_id=node.id, label=node.label,
                    value=node.value.render() if node.value else None,
                    status=node.status.value, confidence=node.confidence,
                    valid_from=node.valid_from, valid_until=node.valid_until,
                    created_at=node.created_at,
                )
            ],
        )
    return timeline(store, *t)


def expired(store: KnowledgeStore, when: datetime | None = None) -> list[KNode]:
    """Live beliefs whose validity window has quietly run out."""
    t = when or utcnow()
    return [
        n for n in store.nodes.values()
        if n.status.is_live and n.valid_until and _aware(n.valid_until) < t
    ]
