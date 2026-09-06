"""Contradiction detection and credibility ranking.

Finding that two documents disagree is the easy half. The useful half is
deciding which one to believe, and noticing when an apparent disagreement is
not one at all -- two claims about the same property over *disjoint* time
windows are a succession, not a conflict, and treating them as a contradiction
is the classic false positive this module exists to avoid.

Four kinds are detected:

* ``NUMERIC``    - same (subject, predicate), values differ beyond tolerance
* ``SEMANTIC``   - same property asserted both ways ("supported"/"deprecated")
* ``TEMPORAL``   - a belief asserted as current after it was superseded
* ``CONSTRAINT`` - an observed value violates a requirement or constraint

Each one is then scored on both sides, so the system can say *which* claim
should stand rather than merely raising a flag.
"""
from __future__ import annotations

import enum
import math
from datetime import datetime, timedelta, timezone
from typing import Iterable

from pydantic import BaseModel, Field

from backend.core.config import settings
from backend.core.models import (
    EdgeType,
    EpistemicStatus,
    KNode,
    NodeType,
    Quantity,
    utcnow,
)
from backend.core.units import relative_delta, same_unit_family, satisfies
from backend.graph.store import KnowledgeStore

# Claims that carry an asserted value and can therefore conflict.
ASSERTIVE = (NodeType.CLAIM, NodeType.OBSERVATION, NodeType.ASSUMPTION)
NORMATIVE = (NodeType.CONSTRAINT, NodeType.REQUIREMENT)


class ContradictionKind(str, enum.Enum):
    NUMERIC = "NUMERIC"
    SEMANTIC = "SEMANTIC"
    TEMPORAL = "TEMPORAL"
    CONSTRAINT = "CONSTRAINT"


class CredibilityBreakdown(BaseModel):
    """Why one side of a disagreement is believed over the other."""

    authority: float = 0.0
    recency: float = 0.0
    confidence: float = 0.0
    corroboration: float = 0.0
    specificity: float = 0.0
    temporal_validity: float = 0.0
    total: float = 0.0

    def as_reason(self) -> str:
        bits = [
            f"authority {self.authority:.2f}",
            f"recency {self.recency:.2f}",
            f"confidence {self.confidence:.2f}",
            f"corroboration {self.corroboration:.2f}",
            f"specificity {self.specificity:.2f}",
            f"temporal validity {self.temporal_validity:.2f}",
        ]
        return ", ".join(bits)


class Contradiction(BaseModel):
    id: str
    kind: ContradictionKind
    left: str                     # node id
    right: str                    # node id
    left_label: str
    right_label: str
    subject: str | None = None
    predicate: str | None = None
    left_value: str | None = None
    right_value: str | None = None
    description: str
    severity: float = 0.5         # 0..1, how much this matters
    detected_at: datetime = Field(default_factory=utcnow)
    left_credibility: CredibilityBreakdown = Field(default_factory=CredibilityBreakdown)
    right_credibility: CredibilityBreakdown = Field(default_factory=CredibilityBreakdown)
    winner: str | None = None     # node id we should believe, or None if contested
    margin: float = 0.0
    verdict: str = ""

    @property
    def loser(self) -> str | None:
        if self.winner is None:
            return None
        return self.right if self.winner == self.left else self.left


# ---------------------------------------------------------------------------
# Credibility
# ---------------------------------------------------------------------------
def _recency_score(node: KNode, now: datetime, half_life_days: float = 120.0) -> float:
    """Exponential decay so a two-year-old report loses to a fresh one."""
    stamps = [p.observed_at for p in node.provenance if p.observed_at]
    latest = max(stamps) if stamps else node.updated_at or node.created_at
    if latest.tzinfo is None:
        latest = latest.replace(tzinfo=timezone.utc)
    age_days = max((now - latest).total_seconds() / 86400.0, 0.0)
    return float(0.5 ** (age_days / half_life_days))


def _corroboration_score(node: KNode, store: KnowledgeStore) -> float:
    """Independent sources and explicit SUPPORTS edges, with sharp diminishing returns."""
    sources = {p.source_path or p.source_id for p in node.provenance if (p.source_path or p.source_id)}
    supports = sum(
        1 for e in store.in_edges(node.id) if e.type is EdgeType.SUPPORTS
    )
    n = len(sources) + supports
    return float(1.0 - math.exp(-0.9 * n)) if n else 0.0


def _specificity_score(node: KNode) -> float:
    """A precise, quantified, time-bounded claim beats a vague one."""
    s = 0.0
    if node.value is not None:
        s += 0.35
        if node.value.is_numeric:
            s += 0.25
        if node.value.unit:
            s += 0.15
    if node.valid_from or node.valid_until:
        s += 0.15
    if node.subject and node.predicate:
        s += 0.10
    return min(s, 1.0)


def _temporal_validity_score(node: KNode, now: datetime) -> float:
    if not node.is_valid_at(now):
        return 0.0
    if node.valid_from and node.valid_until:
        return 1.0
    if node.valid_from:
        return 0.9
    return 0.6                      # open-ended: valid, but unbounded


def credibility(node: KNode, store: KnowledgeStore, now: datetime | None = None) -> CredibilityBreakdown:
    """Score a belief on the six axes the spec calls out."""
    now = now or utcnow()
    authority = max((p.authority for p in node.provenance), default=0.5)
    b = CredibilityBreakdown(
        authority=round(authority, 4),
        recency=round(_recency_score(node, now), 4),
        confidence=round(node.confidence, 4),
        corroboration=round(_corroboration_score(node, store), 4),
        specificity=round(_specificity_score(node), 4),
        temporal_validity=round(_temporal_validity_score(node, now), 4),
    )
    b.total = round(
        0.26 * b.authority
        + 0.22 * b.recency
        + 0.18 * b.confidence
        + 0.14 * b.corroboration
        + 0.10 * b.specificity
        + 0.10 * b.temporal_validity,
        4,
    )
    return b


# ---------------------------------------------------------------------------
# Overlap logic
# ---------------------------------------------------------------------------
def intervals_overlap(a: KNode, b: KNode) -> bool:
    """Do two beliefs claim to hold at the same time?

    Open-ended intervals are treated as extending to infinity in that
    direction, which is what "valid_until: null" means in the vault.
    """
    a_from = a.valid_from or datetime.min.replace(tzinfo=timezone.utc)
    a_to = a.valid_until or datetime.max.replace(tzinfo=timezone.utc)
    b_from = b.valid_from or datetime.min.replace(tzinfo=timezone.utc)
    b_to = b.valid_until or datetime.max.replace(tzinfo=timezone.utc)
    return a_from <= b_to and b_from <= a_to


def _values_conflict(a: Quantity | None, b: Quantity | None) -> tuple[bool, str]:
    """Compare two values, returning (conflicts, human description)."""
    if a is None or b is None:
        return False, ""
    if a.is_numeric and b.is_numeric:
        if not same_unit_family(a, b):
            return False, ""
        delta = relative_delta(a, b)
        if delta is None:
            return a.number != b.number, "values differ"
        if abs(delta) <= settings.contradiction_numeric_tolerance:
            return False, ""
        return True, f"{a.render()} vs {b.render()} ({delta:+.1%})"
    if a.boolean is not None and b.boolean is not None:
        if a.boolean != b.boolean:
            return True, f"{a.render()} vs {b.render()}"
        return False, ""
    ta = (a.text or a.raw or "").strip().lower()
    tb = (b.text or b.raw or "").strip().lower()
    if ta and tb and ta != tb:
        return True, f"'{a.render()}' vs '{b.render()}'"
    return False, ""


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------
def _severity(kind: ContradictionKind, a: KNode, b: KNode, delta: float | None) -> float:
    base = {
        ContradictionKind.CONSTRAINT: 0.9,
        ContradictionKind.NUMERIC: 0.6,
        ContradictionKind.SEMANTIC: 0.7,
        ContradictionKind.TEMPORAL: 0.5,
    }[kind]
    crit = max(a.criticality, b.criticality)
    magnitude = min(abs(delta), 1.0) if delta is not None else 0.5
    return round(min(base * (0.55 + 0.45 * crit) * (0.6 + 0.4 * magnitude), 1.0), 4)


def detect_for_node(
    store: KnowledgeStore, node: KNode, now: datetime | None = None
) -> list[Contradiction]:
    """Every contradiction this node participates in.

    Scoped by the (subject, predicate) index, so this stays cheap enough to run
    on every ingest rather than as a batch job.
    """
    now = now or utcnow()
    found: list[Contradiction] = []
    triple = node.triple()
    if not triple:
        return found
    subject, predicate = triple

    siblings = [
        s for s in store.siblings_of_triple(subject, predicate) if s.id != node.id
    ]

    for other in siblings:
        # ---- assertive vs assertive -----------------------------------
        if node.type in ASSERTIVE and other.type in ASSERTIVE:
            if not (node.status.is_live and other.status.is_live):
                continue
            conflicts, desc = _values_conflict(node.value, other.value)
            if not conflicts:
                continue
            if not intervals_overlap(node, other):
                # Disjoint validity: a succession, not a conflict. The temporal
                # engine turns this into a SUPERSEDES relationship instead.
                continue
            numeric = bool(
                node.value and other.value and node.value.is_numeric and other.value.is_numeric
            )
            kind = ContradictionKind.NUMERIC if numeric else ContradictionKind.SEMANTIC
            delta = relative_delta(other.value, node.value) if numeric else None
            found.append(
                _build(
                    store, kind, other, node,
                    description=f"{subject} / {predicate}: {desc}",
                    severity=_severity(kind, node, other, delta),
                    now=now,
                )
            )

        # ---- observation vs constraint --------------------------------
        elif node.type in ASSERTIVE and other.type in NORMATIVE:
            c = _constraint_violation(store, constraint=other, observed=node, now=now)
            if c:
                found.append(c)
        elif node.type in NORMATIVE and other.type in ASSERTIVE:
            c = _constraint_violation(store, constraint=node, observed=other, now=now)
            if c:
                found.append(c)

    return found


def _constraint_violation(
    store: KnowledgeStore, constraint: KNode, observed: KNode, now: datetime
) -> Contradiction | None:
    if not constraint.operator or constraint.value is None or observed.value is None:
        return None
    if not (constraint.status.is_live and observed.status.is_live):
        return None
    if not intervals_overlap(constraint, observed):
        return None
    ok = satisfies(observed.value, constraint.operator, constraint.value)
    if ok is not False:
        return None
    op = {"LT": "<", "LTE": "<=", "GT": ">", "GTE": ">=", "EQ": "=", "NEQ": "!="}.get(
        constraint.operator.upper(), constraint.operator
    )
    delta = relative_delta(constraint.value, observed.value)
    return _build(
        store,
        ContradictionKind.CONSTRAINT,
        constraint,
        observed,
        description=(
            f"{constraint.subject} / {constraint.predicate}: requires "
            f"{op} {constraint.value.render()}, observed {observed.value.render()}"
        ),
        severity=_severity(ContradictionKind.CONSTRAINT, constraint, observed, delta),
        now=now,
    )


def _build(
    store: KnowledgeStore,
    kind: ContradictionKind,
    left: KNode,
    right: KNode,
    *,
    description: str,
    severity: float,
    now: datetime,
) -> Contradiction:
    lc = credibility(left, store, now)
    rc = credibility(right, store, now)
    margin = round(rc.total - lc.total, 4)
    winner: str | None
    if abs(margin) < 0.05:
        winner, verdict = None, (
            "Too close to call - both beliefs are marked CONTESTED and flagged "
            "for human validation."
        )
    elif margin > 0:
        winner = right.id
        verdict = (
            f"Believe {right.id} ({right.label}): {rc.as_reason()}. "
            f"Outranks {left.id} by {margin:+.3f}."
        )
    else:
        winner = left.id
        verdict = (
            f"Believe {left.id} ({left.label}): {lc.as_reason()}. "
            f"Outranks {right.id} by {-margin:+.3f}."
        )

    return Contradiction(
        id=f"CX-{left.id}-{right.id}",
        kind=kind,
        left=left.id,
        right=right.id,
        left_label=left.label,
        right_label=right.label,
        subject=left.subject or right.subject,
        predicate=left.predicate or right.predicate,
        left_value=left.value.render() if left.value else None,
        right_value=right.value.render() if right.value else None,
        description=description,
        severity=severity,
        detected_at=now,
        left_credibility=lc,
        right_credibility=rc,
        winner=winner,
        margin=abs(margin),
        verdict=verdict,
    )


def detect_all(store: KnowledgeStore, now: datetime | None = None) -> list[Contradiction]:
    """Full sweep. Used by the API and the knowledge-health dashboard."""
    now = now or utcnow()
    seen: set[frozenset[str]] = set()
    out: list[Contradiction] = []
    for node in list(store.nodes.values()):
        if node.type not in ASSERTIVE + NORMATIVE:
            continue
        for c in detect_for_node(store, node, now):
            key = frozenset({c.left, c.right})
            if key in seen:
                continue
            seen.add(key)
            out.append(c)
    out.sort(key=lambda c: -c.severity)
    return out


def status_after(contradiction: Contradiction, node_id: str) -> EpistemicStatus:
    """What a node's epistemic state should become once this is resolved."""
    if contradiction.winner is None:
        return EpistemicStatus.CONTESTED
    if contradiction.winner == node_id:
        return EpistemicStatus.SUPPORTED
    return EpistemicStatus.CONTRADICTED
