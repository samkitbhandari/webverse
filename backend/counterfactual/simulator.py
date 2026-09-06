"""Counterfactual simulation.

"What happens if we redesign the cooling instead of delaying deployment?"

The method is the obvious one done carefully: clone the world, intervene, let
the same reasoning engines run on the clone, and diff the two states. The
clone is detached from Kuzu, so a simulation cannot write to the real graph
even by accident.

What makes the answer useful is that the comparison is not a single number.
A scenario that lowers cost while raising compliance exposure is not "better";
it is a trade, and the report names both sides of it -- cost, schedule,
capacity, risk, compliance, plus the structural health of the knowledge itself
and which decisions each world puts at risk.

First- and second-order effects are distinguished by propagation depth: a node
one hop from the intervention moved because of it, a node four hops away moved
because something else moved.
"""
from __future__ import annotations

import enum
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from backend.core.ids import edge_id, fingerprint, node_id
from backend.core.models import (
    EdgeType,
    EpistemicStatus,
    KEdge,
    KNode,
    NodeType,
    Provenance,
    utcnow,
)
from backend.core.units import parse_value
from backend.graph.store import KnowledgeStore
from backend.reasoning.contradiction import detect_all
from backend.reasoning.criticality import knowledge_health
from backend.reasoning.impact import ImpactReport, propagate
from backend.reasoning.temporal import MOVED, decision_basis
from backend.reasoning.validation import total_uncertainty


class InterventionType(str, enum.Enum):
    SET_VALUE = "SET_VALUE"           # a property takes a different value
    INVALIDATE = "INVALIDATE"         # a belief no longer holds
    ASSERT = "ASSERT"                 # add a new claim
    REMOVE_DEPENDENCY = "REMOVE_DEPENDENCY"
    ADD_DEPENDENCY = "ADD_DEPENDENCY"
    REDIRECT = "REDIRECT"             # move a dependency from one node to another


class Intervention(BaseModel):
    type: InterventionType
    target: str | None = None            # node id
    source: str | None = None            # node id, for edge operations
    subject: str | None = None           # for ASSERT
    predicate: str | None = None
    value: str | None = None
    unit: str | None = None
    edge_type: EdgeType = EdgeType.DEPENDS_ON
    note: str = ""

    def describe(self, store: KnowledgeStore) -> str:
        def label(nid: str | None) -> str:
            n = store.get(nid) if nid else None
            return f"{n.label} ({n.id})" if n else (nid or "?")

        if self.type is InterventionType.SET_VALUE:
            return f"Set {label(self.target)} to {self.value} {self.unit or ''}".strip()
        if self.type is InterventionType.INVALIDATE:
            return f"Invalidate {label(self.target)}"
        if self.type is InterventionType.ASSERT:
            return f"Assert {self.subject} / {self.predicate} = {self.value} {self.unit or ''}".strip()
        if self.type is InterventionType.REMOVE_DEPENDENCY:
            return f"Remove {self.edge_type.value}: {label(self.source)} -> {label(self.target)}"
        if self.type is InterventionType.ADD_DEPENDENCY:
            return f"Add {self.edge_type.value}: {label(self.source)} -> {label(self.target)}"
        return f"Redirect {label(self.source)} from {label(self.target)}"


class DimensionMetrics(BaseModel):
    """The comparison axes the brief calls out, computed from live beliefs."""

    cost: float | None = None
    schedule: float | None = None       # canonical ms
    capacity: float | None = None
    throughput: float | None = None
    risk_exposure: float = 0.0          # sum of severity * confidence
    compliance_violations: int = 0
    open_contradictions: int = 0
    decisions_at_risk: int = 0
    health_score: float = 0.0
    total_uncertainty: float = 0.0
    live_beliefs: int = 0


class DimensionDelta(BaseModel):
    name: str
    before: float | None
    after: float | None
    delta: float | None
    direction: Literal["better", "worse", "unchanged", "unknown"] = "unknown"
    note: str = ""


class ScenarioResult(BaseModel):
    scenario: str
    description: str = ""
    computed_at: datetime = Field(default_factory=utcnow)
    interventions: list[str] = Field(default_factory=list)
    baseline: DimensionMetrics
    counterfactual: DimensionMetrics
    deltas: list[DimensionDelta] = Field(default_factory=list)
    first_order: list[dict] = Field(default_factory=list)
    second_order: list[dict] = Field(default_factory=list)
    newly_at_risk: list[dict] = Field(default_factory=list)
    resolved_decisions: list[dict] = Field(default_factory=list)
    new_contradictions: list[dict] = Field(default_factory=list)
    resolved_contradictions: list[dict] = Field(default_factory=list)
    verdict: str = ""


# ---------------------------------------------------------------------------
# Measurement
# ---------------------------------------------------------------------------
_DIMENSION_PREDICATES: dict[str, tuple[str, ...]] = {
    "cost": ("cost", "budget", "price", "capex", "opex"),
    "schedule": ("lead time", "duration", "delivery time", "charging time"),
    "capacity": ("capacity", "fleet size"),
    "throughput": ("throughput",),
}


def _aggregate(store: KnowledgeStore, predicates: tuple[str, ...]) -> float | None:
    """Sum the live numeric beliefs for a family of predicates.

    The absolute total is not meaningful on its own -- a programme budget and a
    per-vehicle unit cost land in the same bucket. It is used only as a
    *baseline to diff against*, where what matters is how much an intervention
    moves it, and that difference is well defined even when the total is not.
    """
    total, seen = 0.0, False
    for node in store.live_nodes():
        if node.predicate and node.value and node.value.is_numeric:
            if any(p in node.predicate for p in predicates):
                total += node.value.number or 0.0
                seen = True
    return round(total, 4) if seen else None


def measure(store: KnowledgeStore) -> DimensionMetrics:
    health = knowledge_health(store)
    contradictions = detect_all(store)

    risk = 0.0
    for node in store.of_type(NodeType.RISK):
        if node.status.is_live:
            risk += float(node.attributes.get("severity", 0.5)) * node.confidence

    at_risk = 0
    for d in store.of_type(NodeType.DECISION):
        basis = decision_basis(store, d.id)
        if basis and any(n.status in MOVED for n, _ in basis):
            at_risk += 1

    return DimensionMetrics(
        cost=_aggregate(store, _DIMENSION_PREDICATES["cost"]),
        schedule=_aggregate(store, _DIMENSION_PREDICATES["schedule"]),
        capacity=_aggregate(store, _DIMENSION_PREDICATES["capacity"]),
        throughput=_aggregate(store, _DIMENSION_PREDICATES["throughput"]),
        risk_exposure=round(risk, 4),
        compliance_violations=sum(1 for c in contradictions if c.kind.value == "CONSTRAINT"),
        open_contradictions=len(contradictions),
        decisions_at_risk=at_risk,
        health_score=health.score,
        total_uncertainty=total_uncertainty(store),
        live_beliefs=sum(1 for _ in store.live_nodes()),
    )


#: For each metric, does a larger number mean a better or a worse world?
_LOWER_IS_BETTER = {
    "cost", "schedule", "risk_exposure", "compliance_violations",
    "open_contradictions", "decisions_at_risk", "total_uncertainty",
}

#: Descriptive only. A scenario that adds beliefs has not thereby improved the
#: world -- calling that "better" would let any intervention that writes a node
#: claim a win.
_NEUTRAL = {"live_beliefs"}


def _deltas(before: DimensionMetrics, after: DimensionMetrics) -> list[DimensionDelta]:
    out: list[DimensionDelta] = []
    for name in DimensionMetrics.model_fields:
        b = getattr(before, name)
        a = getattr(after, name)
        if b is None and a is None:
            continue
        if name in _NEUTRAL:
            d = None if (b is None or a is None) else round(float(a) - float(b), 4)
            out.append(DimensionDelta(name=name, before=b, after=a, delta=d,
                                      direction="unchanged" if not d else "unknown",
                                      note="informational: neither better nor worse"))
            continue
        if b is None or a is None:
            out.append(DimensionDelta(name=name, before=b, after=a, delta=None,
                                      direction="unknown",
                                      note="not measurable in one of the two worlds"))
            continue
        d = round(float(a) - float(b), 4)
        if abs(d) < 1e-9:
            direction = "unchanged"
        elif name in _LOWER_IS_BETTER:
            direction = "better" if d < 0 else "worse"
        else:
            direction = "better" if d > 0 else "worse"
        out.append(DimensionDelta(name=name, before=b, after=a, delta=d, direction=direction))
    return out


# ---------------------------------------------------------------------------
# Application
# ---------------------------------------------------------------------------
def apply_intervention(store: KnowledgeStore, iv: Intervention) -> list[str]:
    """Mutate the (cloned) world. Returns the node ids that were disturbed."""
    touched: list[str] = []
    prov = Provenance(source_label="counterfactual scenario", authority=0.5,
                      extractor="simulation")

    if iv.type is InterventionType.SET_VALUE and iv.target:
        node = store.get(iv.target)
        if node:
            store.update_node(iv.target, value=parse_value(iv.value, iv.unit or
                                                           (node.value.unit if node.value else None)))
            touched.append(iv.target)

    elif iv.type is InterventionType.INVALIDATE and iv.target:
        node = store.get(iv.target)
        if node:
            store.update_node(iv.target, status=EpistemicStatus.INVALIDATED,
                              valid_until=utcnow())
            touched.append(iv.target)

    elif iv.type is InterventionType.ASSERT and iv.subject and iv.predicate:
        node = KNode(
            id=node_id(NodeType.CLAIM), type=NodeType.CLAIM,
            label=f"{iv.subject} {iv.predicate} = {iv.value}"[:110],
            body=iv.note or "Asserted by counterfactual scenario",
            subject=iv.subject, predicate=iv.predicate.lower(),
            value=parse_value(iv.value, iv.unit),
            status=EpistemicStatus.PROBABLE, confidence=0.6,
            valid_from=utcnow(),
            fingerprint=fingerprint("cf", iv.subject, iv.predicate, str(iv.value)),
            provenance=[prov],
        )
        store.add_node(node)
        touched.append(node.id)
        if entity := store.find_entity(iv.subject):
            store.add_edge(KEdge(id=edge_id(), source=node.id, target=entity.id,
                                 type=EdgeType.ABOUT, weight=0.6, confidence=0.8))

    elif iv.type is InterventionType.REMOVE_DEPENDENCY and iv.source and iv.target:
        if edge := store.find_edge(iv.source, iv.target, iv.edge_type):
            store.remove_edge(edge.id)
            touched += [iv.source, iv.target]

    elif iv.type is InterventionType.ADD_DEPENDENCY and iv.source and iv.target:
        store.add_edge(KEdge(
            id=edge_id(), source=iv.source, target=iv.target, type=iv.edge_type,
            weight=0.8, confidence=0.7,
            rationale=iv.note or "added by counterfactual scenario",
        ))
        touched += [iv.source, iv.target]

    elif iv.type is InterventionType.REDIRECT and iv.source and iv.target:
        # cut every dependency out of `source` and point it at `target`
        for e in list(store.out_edges(iv.source)):
            if e.type in (EdgeType.DEPENDS_ON, EdgeType.BASED_ON, EdgeType.REQUIRES):
                store.remove_edge(e.id)
                touched.append(e.target)
        store.add_edge(KEdge(
            id=edge_id(), source=iv.source, target=iv.target, type=iv.edge_type,
            weight=0.8, confidence=0.7,
            rationale=iv.note or "redirected by counterfactual scenario",
        ))
        touched += [iv.source, iv.target]

    return [t for t in touched if t in store.nodes]


def simulate(
    store: KnowledgeStore,
    interventions: list[Intervention],
    *,
    name: str = "scenario",
    description: str = "",
    max_depth: int = 5,
) -> ScenarioResult:
    """Run one scenario against a clone of the current world."""
    baseline = measure(store)
    baseline_contradictions = {c.id: c for c in detect_all(store)}
    baseline_at_risk = _decisions_at_risk(store)

    world = store.clone()
    touched: list[str] = []
    for iv in interventions:
        touched += apply_intervention(world, iv)

    after = measure(world)
    after_contradictions = {c.id: c for c in detect_all(world)}
    after_at_risk = _decisions_at_risk(world)

    report: ImpactReport | None = None
    if touched:
        report = propagate(world, {t: 1.0 for t in dict.fromkeys(touched)},
                           max_depth=max_depth)

    result = ScenarioResult(
        scenario=name,
        description=description,
        interventions=[iv.describe(store) for iv in interventions],
        baseline=baseline,
        counterfactual=after,
        deltas=_deltas(baseline, after),
    )

    if report:
        for a in report.affected:
            row = {
                "id": a.id, "label": a.label, "type": a.type,
                "severity": a.severity, "depth": a.depth,
                "explanation": a.explanation,
            }
            (result.first_order if a.depth <= 1 else result.second_order).append(row)
        result.first_order = result.first_order[:12]
        result.second_order = result.second_order[:12]

    result.newly_at_risk = [
        {"id": d, "label": (world.get(d).label if world.get(d) else d)}
        for d in sorted(after_at_risk - baseline_at_risk)
    ]
    result.resolved_decisions = [
        {"id": d, "label": (store.get(d).label if store.get(d) else d)}
        for d in sorted(baseline_at_risk - after_at_risk)
    ]
    result.new_contradictions = [
        after_contradictions[k].model_dump(mode="json")
        for k in sorted(set(after_contradictions) - set(baseline_contradictions))
    ]
    result.resolved_contradictions = [
        baseline_contradictions[k].model_dump(mode="json")
        for k in sorted(set(baseline_contradictions) - set(after_contradictions))
    ]
    result.verdict = _verdict(result)
    return result


def _decisions_at_risk(store: KnowledgeStore) -> set[str]:
    out: set[str] = set()
    for d in store.of_type(NodeType.DECISION):
        basis = decision_basis(store, d.id)
        if basis and any(n.status in MOVED for n, _ in basis):
            out.add(d.id)
    return out


def _verdict(r: ScenarioResult) -> str:
    better = [d.name for d in r.deltas if d.direction == "better"]
    worse = [d.name for d in r.deltas if d.direction == "worse"]
    bits: list[str] = []

    if r.resolved_contradictions:
        bits.append(f"resolves {len(r.resolved_contradictions)} contradiction(s)")
    if r.new_contradictions:
        bits.append(f"introduces {len(r.new_contradictions)} new contradiction(s)")
    if r.resolved_decisions:
        bits.append(f"takes {len(r.resolved_decisions)} decision(s) out of risk")
    if r.newly_at_risk:
        bits.append(f"puts {len(r.newly_at_risk)} decision(s) at risk")

    health_delta = r.counterfactual.health_score - r.baseline.health_score
    bits.append(f"knowledge health {r.baseline.health_score:.1f} -> "
                f"{r.counterfactual.health_score:.1f} ({health_delta:+.1f})")

    if r.first_order or r.second_order:
        bits.append(f"{len(r.first_order)} first-order and "
                    f"{len(r.second_order)} second-order effect(s)")

    trade = ""
    if better and worse:
        trade = (f" It is a trade, not a win: better on {', '.join(better[:3])}, "
                 f"worse on {', '.join(worse[:3])}.")
    elif better and not worse:
        trade = f" Improves {', '.join(better[:4])} with no measured regression."
    elif worse and not better:
        trade = f" Regresses {', '.join(worse[:4])} with no measured gain."

    return f"{r.scenario}: " + "; ".join(bits) + "." + trade


def compare(results: list[ScenarioResult]) -> dict[str, Any]:
    """Rank alternative scenarios side by side."""
    def score(r: ScenarioResult) -> float:
        s = r.counterfactual.health_score
        s -= 6.0 * len(r.new_contradictions)
        s -= 8.0 * len(r.newly_at_risk)
        s += 5.0 * len(r.resolved_decisions)
        s += 4.0 * len(r.resolved_contradictions)
        s -= 3.0 * r.counterfactual.risk_exposure
        return round(s, 3)

    ranked = sorted(results, key=score, reverse=True)
    return {
        "ranking": [
            {
                "scenario": r.scenario,
                "score": score(r),
                "health": r.counterfactual.health_score,
                "new_contradictions": len(r.new_contradictions),
                "resolved_contradictions": len(r.resolved_contradictions),
                "decisions_at_risk": r.counterfactual.decisions_at_risk,
                "risk_exposure": r.counterfactual.risk_exposure,
                "verdict": r.verdict,
            }
            for r in ranked
        ],
        "recommended": ranked[0].scenario if ranked else None,
        "rationale": (
            f"{ranked[0].scenario} ranks highest: {ranked[0].verdict}"
            if ranked else "No scenarios were simulated."
        ),
    }
