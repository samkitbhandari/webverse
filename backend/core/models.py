"""The NEXUS Omega knowledge model.

NEXUS does not think in files and folders. It thinks in entities, claims,
decisions, requirements, constraints, events, risks and evidence -- each one
carrying provenance, confidence and a validity interval. This module is the
single definition of that vocabulary; every other layer imports from here.
"""
from __future__ import annotations

import enum
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Vocabulary
# ---------------------------------------------------------------------------
class NodeType(str, enum.Enum):
    ENTITY = "Entity"
    CLAIM = "Claim"
    DECISION = "Decision"
    REQUIREMENT = "Requirement"
    CONSTRAINT = "Constraint"
    ASSUMPTION = "Assumption"
    OBSERVATION = "Observation"
    EVENT = "Event"
    RISK = "Risk"
    ACTION = "Action"
    EVIDENCE = "Evidence"
    #: An emergent topic discovered by clustering embeddings, not asserted by
    #: any source. Concepts are derived structure: they can be rebuilt from
    #: scratch at any time and nothing should depend on a particular one.
    CONCEPT = "Concept"


class EdgeType(str, enum.Enum):
    SUPPORTS = "SUPPORTS"
    CONTRADICTS = "CONTRADICTS"
    DEPENDS_ON = "DEPENDS_ON"
    REQUIRES = "REQUIRES"
    CAUSES = "CAUSES"
    AFFECTS = "AFFECTS"
    IMPLEMENTS = "IMPLEMENTS"
    SUPERSEDES = "SUPERSEDES"
    DERIVED_FROM = "DERIVED_FROM"
    OWNED_BY = "OWNED_BY"
    CONSTRAINS = "CONSTRAINS"
    INVALIDATES = "INVALIDATES"
    RELATED_TO = "RELATED_TO"
    BASED_ON = "BASED_ON"     # Decision -> Claim  (decision lineage)
    ABOUT = "ABOUT"           # Claim -> Entity
    MEMBER_OF = "MEMBER_OF"   # Claim -> Concept   (embedding cluster membership)


class EpistemicStatus(str, enum.Enum):
    """How strongly the system currently holds a belief."""

    SUPPORTED = "SUPPORTED"
    PROBABLE = "PROBABLE"
    UNCERTAIN = "UNCERTAIN"
    CONTESTED = "CONTESTED"
    CONTRADICTED = "CONTRADICTED"
    SUPERSEDED = "SUPERSEDED"
    INVALIDATED = "INVALIDATED"
    UNVERIFIED = "UNVERIFIED"

    @property
    def is_live(self) -> bool:
        """Whether a belief in this state should still influence reasoning."""
        return self not in (EpistemicStatus.SUPERSEDED, EpistemicStatus.INVALIDATED)


#: How much a relationship transmits change to its target. Used by the impact
#: engine as the base multiplier for each edge on a propagation path.
EDGE_INFLUENCE: dict[EdgeType, float] = {
    EdgeType.INVALIDATES: 1.00,
    EdgeType.CONTRADICTS: 0.95,
    EdgeType.SUPERSEDES: 0.95,
    EdgeType.REQUIRES: 0.90,
    EdgeType.DEPENDS_ON: 0.85,
    EdgeType.BASED_ON: 0.85,
    EdgeType.CONSTRAINS: 0.80,
    EdgeType.CAUSES: 0.80,
    EdgeType.IMPLEMENTS: 0.70,
    EdgeType.AFFECTS: 0.65,
    EdgeType.SUPPORTS: 0.60,
    EdgeType.DERIVED_FROM: 0.55,
    EdgeType.ABOUT: 0.35,
    EdgeType.OWNED_BY: 0.25,
    EdgeType.RELATED_TO: 0.20,
    # Cluster membership is topical resemblance, not dependency. A concept
    # touching twenty claims would otherwise become a superhighway through
    # which every claim reaches every other, and impact analysis would
    # degenerate into "everything affects everything". Kept deliberately low.
    EdgeType.MEMBER_OF: 0.08,
}

#: Which way a change travels across a relationship.
#:
#: This is the single most load-bearing table in the impact engine and it is
#: not the same as the edge's own orientation. "Decision D BASED_ON Claim C"
#: points D -> C, but a change in C is what disturbs D, so impact flows
#: *backward* along it. Getting this wrong makes the engine propagate
#: confidently in exactly the wrong direction.
EDGE_DIRECTION: dict[EdgeType, str] = {
    # change in the TARGET disturbs the SOURCE
    EdgeType.DEPENDS_ON: "backward",
    EdgeType.BASED_ON: "backward",
    EdgeType.REQUIRES: "backward",
    EdgeType.DERIVED_FROM: "backward",
    EdgeType.IMPLEMENTS: "backward",
    # change in the SOURCE disturbs the TARGET
    EdgeType.SUPPORTS: "forward",
    EdgeType.CONTRADICTS: "forward",
    EdgeType.INVALIDATES: "forward",
    EdgeType.SUPERSEDES: "forward",
    EdgeType.CAUSES: "forward",
    EdgeType.AFFECTS: "forward",
    EdgeType.CONSTRAINS: "forward",
    # symmetric, weak
    EdgeType.ABOUT: "both",
    EdgeType.OWNED_BY: "both",
    EdgeType.RELATED_TO: "both",
    EdgeType.MEMBER_OF: "both",
}

#: Intrinsic weight of a node type when scoring criticality. A decision or a
#: requirement matters more than a loose observation.
NODE_CRITICALITY: dict[NodeType, float] = {
    NodeType.DECISION: 1.00,
    NodeType.REQUIREMENT: 0.95,
    NodeType.CONSTRAINT: 0.90,
    NodeType.RISK: 0.85,
    NodeType.ACTION: 0.75,
    NodeType.ASSUMPTION: 0.70,
    NodeType.CLAIM: 0.60,
    NodeType.ENTITY: 0.50,
    NodeType.EVENT: 0.50,
    NodeType.OBSERVATION: 0.45,
    NodeType.EVIDENCE: 0.30,
    # Derived, not asserted: a concept should never outrank the beliefs that
    # produced it when the graph is ranked by criticality.
    NodeType.CONCEPT: 0.25,
}


# ---------------------------------------------------------------------------
# Values and provenance
# ---------------------------------------------------------------------------
_DURATION_STEPS: list[tuple[float, str]] = [
    (2.592e9, "months"), (6.048e8, "weeks"), (8.64e7, "days"),
    (3.6e6, "hours"), (6e4, "minutes"), (1e3, "seconds"),
]


_DURATION_FACTORS: dict[str, float] = {
    "month": 2.592e9, "week": 6.048e8, "day": 8.64e7,
    "hour": 3.6e6, "hr": 3.6e6, "minute": 6e4, "min": 6e4,
    "second": 1e3, "sec": 1e3, "ms": 1.0,
}


def _render_duration(ms: float, raw: str = "") -> str:
    """Render a canonical millisecond duration the way a reader expects.

    If the source said "14 days", say "14 days" -- not "2 weeks". They are the
    same duration, but silently re-expressing a document's own figure makes the
    value harder to reconcile against the source it came from. Only values with
    no original phrasing (a computed or simulated one) fall back to picking the
    largest unit that fits.
    """
    low = raw.lower()
    for name, factor in _DURATION_FACTORS.items():
        if name in low:
            value = ms / factor
            unit = "ms" if name == "ms" else f"{name}s" if abs(value) != 1 else name
            return f"{value:,.10g} {unit}"

    for factor, name in _DURATION_STEPS:
        if abs(ms) >= factor:
            value = ms / factor
            singular = name[:-1] if abs(value) == 1 else name
            return f"{value:,.10g} {singular}"
    return f"{ms:,.10g} ms"


class Quantity(BaseModel):
    """A claim's value, normalised so two sources can actually be compared.

    "50,000 units/month" and "50k units per month" both land on
    number=50000.0, unit="unit/month" -- which is what makes numeric
    contradiction detection possible at all.
    """

    raw: str = ""
    number: float | None = None
    unit: str | None = None
    boolean: bool | None = None
    text: str | None = None

    @property
    def is_numeric(self) -> bool:
        return self.number is not None

    def render(self) -> str:
        """Human-readable form.

        Durations are stored canonically in milliseconds so that "14 days" and
        "2 weeks" compare correctly, but showing a lead time as
        "1,209,600,000 ms" is useless to a reader, so the canonical value is
        rendered back into the largest unit that divides it cleanly.
        """
        if self.number is not None:
            if self.unit == "ms":
                return _render_duration(self.number, self.raw)
            n = f"{self.number:,.10g}"
            return f"{n} {self.unit}" if self.unit else n
        if self.boolean is not None:
            return "true" if self.boolean else "false"
        return self.text or self.raw


class Provenance(BaseModel):
    """Where a piece of knowledge came from, and how much we trust that."""

    source_id: str | None = None          # evidence node id
    source_path: str | None = None        # original file
    source_label: str | None = None
    locator: str | None = None            # section / chunk anchor
    excerpt: str | None = None            # the sentence we extracted from
    authority: float = 0.6                # 0..1 trustworthiness of the source
    observed_at: datetime = Field(default_factory=utcnow)
    extractor: str = "unknown"            # which pipeline produced this


# ---------------------------------------------------------------------------
# Graph primitives
# ---------------------------------------------------------------------------
class KNode(BaseModel):
    """A node in the temporal knowledge graph."""

    id: str
    type: NodeType
    label: str
    body: str = ""

    # claim/observation/constraint structure -- the part machines reason over
    subject: str | None = None            # canonical entity key
    predicate: str | None = None          # property being asserted
    value: Quantity | None = None
    operator: str | None = None           # constraints: LT/LTE/GT/GTE/EQ/NEQ

    # epistemics
    status: EpistemicStatus = EpistemicStatus.UNVERIFIED
    confidence: float = 0.5

    # temporal validity
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)
    valid_from: datetime | None = None
    valid_until: datetime | None = None

    provenance: list[Provenance] = Field(default_factory=list)
    fingerprint: str = ""                 # content hash, for idempotent ingest
    tags: list[str] = Field(default_factory=list)
    attributes: dict[str, Any] = Field(default_factory=dict)

    @field_validator("confidence")
    @classmethod
    def _clamp(cls, v: float) -> float:
        return max(0.0, min(1.0, v))

    @property
    def criticality(self) -> float:
        """Intrinsic importance before graph structure is considered."""
        base = NODE_CRITICALITY.get(self.type, 0.5)
        return float(self.attributes.get("criticality", base))

    def is_valid_at(self, when: datetime) -> bool:
        if self.valid_from and when < self.valid_from:
            return False
        if self.valid_until and when > self.valid_until:
            return False
        return True

    def triple(self) -> tuple[str, str] | None:
        """The (subject, predicate) key two claims must share to conflict."""
        if self.subject and self.predicate:
            return (self.subject.strip().lower(), self.predicate.strip().lower())
        return None


class KEdge(BaseModel):
    """A typed, weighted, time-bounded relationship."""

    id: str
    source: str
    target: str
    type: EdgeType
    weight: float = 1.0                   # relationship strength, 0..1
    confidence: float = 0.7
    created_at: datetime = Field(default_factory=utcnow)
    valid_from: datetime | None = None
    valid_until: datetime | None = None
    provenance: Provenance | None = None
    rationale: str = ""
    attributes: dict[str, Any] = Field(default_factory=dict)

    @property
    def influence(self) -> float:
        """Fraction of a change that crosses this edge (used by impact)."""
        return EDGE_INFLUENCE.get(self.type, 0.3) * self.weight * self.confidence

    def is_valid_at(self, when: datetime) -> bool:
        if self.valid_from and when < self.valid_from:
            return False
        if self.valid_until and when > self.valid_until:
            return False
        return True


# ---------------------------------------------------------------------------
# Events (Section 27: one canonical envelope, many semantic events)
# ---------------------------------------------------------------------------
class SourceEventType(str, enum.Enum):
    SOURCE_CREATED = "SOURCE_CREATED"
    SOURCE_MODIFIED = "SOURCE_MODIFIED"
    SOURCE_DELETED = "SOURCE_DELETED"
    SOURCE_MOVED = "SOURCE_MOVED"


class SemanticEventType(str, enum.Enum):
    ENTITY_ADDED = "ENTITY_ADDED"
    ENTITY_MERGED = "ENTITY_MERGED"
    CLAIM_ADDED = "CLAIM_ADDED"
    CLAIM_CHANGED = "CLAIM_CHANGED"
    CLAIM_SUPERSEDED = "CLAIM_SUPERSEDED"
    CLAIM_INVALIDATED = "CLAIM_INVALIDATED"
    RELATIONSHIP_ADDED = "RELATIONSHIP_ADDED"
    CONTRADICTION_DETECTED = "CONTRADICTION_DETECTED"
    CONSTRAINT_VIOLATED = "CONSTRAINT_VIOLATED"
    KNOWLEDGE_DRIFT = "KNOWLEDGE_DRIFT"
    IMPACT_COMPUTED = "IMPACT_COMPUTED"
    DECISION_AFFECTED = "DECISION_AFFECTED"
    GRAPH_CHANGED = "GRAPH_CHANGED"
    PIPELINE_STAGE = "PIPELINE_STAGE"


class CanonicalEvent(BaseModel):
    """Every change entering the system is normalised into this envelope."""

    event_id: str
    event_type: SourceEventType | SemanticEventType
    source_id: str | None = None
    timestamp: datetime = Field(default_factory=utcnow)
    payload: dict[str, Any] = Field(default_factory=dict)

    def ws(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "event_type": self.event_type.value,
            "source_id": self.source_id,
            "timestamp": self.timestamp.isoformat(),
            "payload": self.payload,
        }


# ---------------------------------------------------------------------------
# LLM extraction contract (structured output schema)
# ---------------------------------------------------------------------------
class ExtractedEntity(BaseModel):
    name: str
    kind: str = "Concept"                 # Person/Org/System/Product/...
    description: str = ""
    aliases: list[str] = Field(default_factory=list)


class ExtractedClaim(BaseModel):
    text: str
    subject: str
    predicate: str
    value: str = ""
    unit: str | None = None
    confidence: float = 0.7
    valid_from: str | None = None
    valid_until: str | None = None
    kind: Literal["Claim", "Observation", "Assumption"] = "Claim"


class ExtractedConstraint(BaseModel):
    text: str
    subject: str
    predicate: str
    operator: Literal["LT", "LTE", "GT", "GTE", "EQ", "NEQ"] = "LTE"
    value: str
    unit: str | None = None
    kind: Literal["Constraint", "Requirement"] = "Constraint"
    confidence: float = 0.75


class ExtractedDecision(BaseModel):
    text: str
    subject: str = ""
    rationale: str = ""
    based_on: list[str] = Field(default_factory=list)   # claim texts/subjects
    confidence: float = 0.7


class ExtractedEvent(BaseModel):
    text: str
    subject: str = ""
    occurred_at: str | None = None
    affects: list[str] = Field(default_factory=list)
    confidence: float = 0.7


class ExtractedRisk(BaseModel):
    text: str
    subject: str = ""
    severity: float = 0.5
    confidence: float = 0.6


class ExtractedRelationship(BaseModel):
    source: str                            # label of a node in this extraction
    target: str
    type: EdgeType = EdgeType.RELATED_TO
    rationale: str = ""
    weight: float = 0.7
    confidence: float = 0.7


class Extraction(BaseModel):
    """What the semantic compiler returns for one chunk of a source."""

    entities: list[ExtractedEntity] = Field(default_factory=list)
    claims: list[ExtractedClaim] = Field(default_factory=list)
    constraints: list[ExtractedConstraint] = Field(default_factory=list)
    decisions: list[ExtractedDecision] = Field(default_factory=list)
    events: list[ExtractedEvent] = Field(default_factory=list)
    risks: list[ExtractedRisk] = Field(default_factory=list)
    relationships: list[ExtractedRelationship] = Field(default_factory=list)

    def merge(self, other: "Extraction") -> "Extraction":
        return Extraction(
            entities=self.entities + other.entities,
            claims=self.claims + other.claims,
            constraints=self.constraints + other.constraints,
            decisions=self.decisions + other.decisions,
            events=self.events + other.events,
            risks=self.risks + other.risks,
            relationships=self.relationships + other.relationships,
        )

    def is_empty(self) -> bool:
        return not any(
            (
                self.entities,
                self.claims,
                self.constraints,
                self.decisions,
                self.events,
                self.risks,
            )
        )
