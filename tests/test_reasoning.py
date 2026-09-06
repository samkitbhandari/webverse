"""The reasoning kernel: contradiction, temporal succession, impact, validation."""
from __future__ import annotations

from datetime import timedelta

import pytest

from backend.core.models import EdgeType, EpistemicStatus, NodeType, Provenance
from backend.core.units import parse_value
from backend.reasoning import temporal
from backend.reasoning.contradiction import (
    ContradictionKind, credibility, detect_all, detect_for_node, intervals_overlap,
)
from backend.reasoning.criticality import knowledge_health, rank_critical
from backend.reasoning.impact import propagate
from backend.reasoning.validation import build_queue


# ---------------------------------------------------------------------------
# Contradiction
# ---------------------------------------------------------------------------
def test_constraint_violation_is_detected(battery_world, make_node, now):
    """An observation exceeding a requirement is the headline finding."""
    w = battery_world
    make_node(
        NodeType.REQUIREMENT, "BP-7 limit 60C",
        subject="Battery Pack BP-7", predicate="max operating temperature",
        operator="LTE", value=parse_value("60 degC"), valid_from=now,
        confidence=0.95, provenance=[Provenance(source_label="regulation", authority=0.95)],
    )
    found = detect_all(w["store"])
    kinds = {c.kind for c in found}
    assert ContradictionKind.CONSTRAINT in kinds
    violation = next(c for c in found if c.kind is ContradictionKind.CONSTRAINT)
    assert "60 degC" in violation.description and "68 degC" in violation.description


def test_disjoint_validity_is_succession_not_conflict(store, make_node, now):
    """Two values for one property over non-overlapping windows do not conflict."""
    make_node(
        NodeType.CLAIM, "old capacity", subject="Supplier A", predicate="capacity",
        value=parse_value("50000 units/month"),
        valid_from=now - timedelta(days=400), valid_until=now - timedelta(days=10),
    )
    make_node(
        NodeType.CLAIM, "new capacity", subject="Supplier A", predicate="capacity",
        value=parse_value("31000 units/month"), valid_from=now - timedelta(days=5),
    )
    assert detect_all(store) == []


def test_overlapping_validity_is_a_conflict(store, make_node, now):
    make_node(
        NodeType.CLAIM, "capacity A", subject="Supplier A", predicate="capacity",
        value=parse_value("50000 units/month"), valid_from=now - timedelta(days=40),
    )
    make_node(
        NodeType.CLAIM, "capacity B", subject="Supplier A", predicate="capacity",
        value=parse_value("31000 units/month"), valid_from=now - timedelta(days=5),
    )
    found = detect_all(store)
    assert len(found) == 1
    assert found[0].kind is ContradictionKind.NUMERIC


def test_values_within_tolerance_are_not_a_conflict(store, make_node, now):
    make_node(NodeType.CLAIM, "a", subject="S", predicate="capacity",
              value=parse_value("50000 units/month"), valid_from=now)
    make_node(NodeType.CLAIM, "b", subject="S", predicate="capacity",
              value=parse_value("50100 units/month"), valid_from=now)
    assert detect_all(store) == []


def test_credibility_prefers_the_more_authoritative_recent_source(store, make_node, now):
    weak = make_node(
        NodeType.CLAIM, "weak", subject="S", predicate="capacity",
        value=parse_value("50000 units/month"), valid_from=now, confidence=0.6,
        provenance=[Provenance(source_label="memo", authority=0.5,
                               observed_at=now - timedelta(days=400))],
    )
    strong = make_node(
        NodeType.CLAIM, "strong", subject="S", predicate="capacity",
        value=parse_value("31000 units/month"), valid_from=now, confidence=0.95,
        provenance=[Provenance(source_label="contract", authority=0.95, observed_at=now)],
    )
    found = detect_for_node(store, strong)
    assert len(found) == 1
    assert found[0].winner == strong.id
    assert credibility(strong, store).total > credibility(weak, store).total


def test_open_ended_intervals_overlap(store, make_node, now):
    a = make_node(NodeType.CLAIM, "a", subject="S", predicate="p", valid_from=now)
    b = make_node(NodeType.CLAIM, "b", subject="S", predicate="p")
    assert intervals_overlap(a, b)


# ---------------------------------------------------------------------------
# Temporal
# ---------------------------------------------------------------------------
def test_supersession_closes_the_old_validity_window(battery_world, make_node, now):
    w = battery_world
    newer = make_node(
        NodeType.REQUIREMENT, "BP-7 limit 60C",
        subject="Battery Pack BP-7", predicate="max operating temperature",
        operator="LTE", value=parse_value("60 degC"), valid_from=now,
    )
    preds = temporal.find_predecessors(w["store"], newer)
    assert w["req"].id in {p.id for p in preds}

    temporal.supersede(w["store"], newer, w["req"])
    old = w["store"].get(w["req"].id)
    assert old.status is EpistemicStatus.SUPERSEDED
    assert old.valid_until is not None
    assert w["store"].find_edge(newer.id, old.id, EdgeType.SUPERSEDES) is not None


def test_timeline_orders_beliefs_oldest_first(battery_world, make_node, now):
    w = battery_world
    make_node(
        NodeType.REQUIREMENT, "BP-7 limit 60C",
        subject="Battery Pack BP-7", predicate="max operating temperature",
        operator="LTE", value=parse_value("60 degC"), valid_from=now,
    )
    tl = temporal.timeline(w["store"], "Battery Pack BP-7", "max operating temperature")
    assert [p.value for p in tl.points][0] == "70 degC"
    assert len(tl.points) == 3


def test_decision_basis_includes_incoming_constraints(battery_world):
    """A requirement constraining a decision is part of what it rests on."""
    w = battery_world
    basis = temporal.decision_basis(w["store"], w["decision"].id)
    ids = {n.id for n, _ in basis}
    assert w["req"].id in ids       # incoming CONSTRAINS
    assert w["arch"].id in ids      # outgoing BASED_ON


def test_decision_flagged_at_risk_once_its_basis_moves(battery_world, make_node, now):
    w = battery_world
    lineage = temporal.beliefs_behind_decision(w["store"], w["decision"].id)
    assert lineage["still_valid"]

    newer = make_node(
        NodeType.REQUIREMENT, "BP-7 limit 60C",
        subject="Battery Pack BP-7", predicate="max operating temperature",
        operator="LTE", value=parse_value("60 degC"), valid_from=now,
    )
    temporal.supersede(w["store"], newer, w["req"])

    lineage = temporal.beliefs_behind_decision(w["store"], w["decision"].id)
    assert not lineage["still_valid"]
    assert any(b["id"] == w["req"].id for b in lineage["changed_since"])


def test_backfilled_knowledge_counts_as_known_at_decision_time(battery_world):
    """A source ingested today can assert a requirement in force last year."""
    w = battery_world
    lineage = temporal.beliefs_behind_decision(w["store"], w["decision"].id)
    req_row = next(b for b in lineage["basis"] if b["id"] == w["req"].id)
    assert req_row["believed_at_decision_time"]


# ---------------------------------------------------------------------------
# Impact
# ---------------------------------------------------------------------------
def test_impact_flows_backward_along_dependency_edges(battery_world):
    """R CONSTRAINS D points R->D, but D IMPLEMENTS-> R must still be reached."""
    w = battery_world
    report = propagate(w["store"], [w["req"].id])
    reached = {a.id for a in report.affected}
    assert w["decision"].id in reached      # forward along CONSTRAINS
    assert w["arch"].id in reached          # backward along IMPLEMENTS


def test_impact_attenuates_with_distance(battery_world):
    w = battery_world
    report = propagate(w["store"], [w["req"].id])
    by_id = {a.id: a for a in report.affected}
    near = by_id[w["decision"].id]
    far = by_id[w["pack"].id]
    assert near.depth <= far.depth or near.score >= far.score


def test_impact_explains_itself_with_a_path(battery_world):
    w = battery_world
    report = propagate(w["store"], [w["req"].id])
    hit = next(a for a in report.affected if a.id == w["decision"].id)
    assert hit.path[0] == w["req"].id and hit.path[-1] == w["decision"].id
    assert "CONSTRAINS" in hit.explanation
    assert hit.hops and all(0 < h.influence <= 1 for h in hit.hops)


def test_impact_reports_affected_decisions_separately(battery_world):
    w = battery_world
    report = propagate(w["store"], [w["req"].id])
    assert w["decision"].id in {d.id for d in report.affected_decisions}


def test_unknown_seed_yields_an_explained_empty_report(store):
    report = propagate(store, ["nope"])
    assert report.total_affected == 0 and report.notes


def test_seed_magnitude_scales_the_result(battery_world):
    w = battery_world
    big = propagate(w["store"], {w["req"].id: 1.0})
    small = propagate(w["store"], {w["req"].id: 0.2})
    big_score = next(a.score for a in big.affected if a.id == w["decision"].id)
    small_score = next(a.score for a in small.affected if a.id == w["decision"].id)
    assert small_score < big_score


def test_propagation_reaches_through_a_retired_node(battery_world, make_node, now):
    """A superseded requirement is exactly why its dependents are disturbed."""
    w = battery_world
    newer = make_node(
        NodeType.REQUIREMENT, "BP-7 limit 60C",
        subject="Battery Pack BP-7", predicate="max operating temperature",
        operator="LTE", value=parse_value("60 degC"), valid_from=now,
    )
    temporal.supersede(w["store"], newer, w["req"])
    report = propagate(w["store"], [newer.id])
    assert w["decision"].id in {a.id for a in report.affected}


# ---------------------------------------------------------------------------
# Health and validation
# ---------------------------------------------------------------------------
def test_health_degrades_when_a_decision_loses_its_footing(battery_world, make_node, now):
    w = battery_world
    before = knowledge_health(w["store"])
    newer = make_node(
        NodeType.REQUIREMENT, "BP-7 limit 60C",
        subject="Battery Pack BP-7", predicate="max operating temperature",
        operator="LTE", value=parse_value("60 degC"), valid_from=now,
        provenance=[Provenance(source_label="regulation", authority=0.95, observed_at=now)],
    )
    temporal.supersede(w["store"], newer, w["req"])
    after = knowledge_health(w["store"])
    assert after.score < before.score
    assert after.decisions_at_risk >= 1


def test_critical_ranking_favours_depended_upon_nodes(battery_world):
    ranked = rank_critical(battery_world["store"], top_k=10)
    assert ranked
    assert ranked == sorted(ranked, key=lambda c: -c.criticality)


def test_validation_queue_ranks_by_leverage(store, make_node, make_edge, now):
    lonely = make_node(NodeType.CLAIM, "isolated doubt", subject="X", predicate="p",
                       status=EpistemicStatus.UNVERIFIED, confidence=0.3)
    pivotal = make_node(NodeType.CLAIM, "load-bearing doubt", subject="Y", predicate="q",
                        status=EpistemicStatus.UNVERIFIED, confidence=0.3)
    for i in range(4):
        d = make_node(NodeType.DECISION, f"decision {i}")
        make_edge(d, pivotal, EdgeType.BASED_ON)

    queue = build_queue(store, limit=5)
    ids = [c.id for c in queue.candidates]
    assert ids.index(pivotal.id) < ids.index(lonely.id)
    top = queue.candidates[0]
    assert top.id == pivotal.id and top.downstream_decisions == 4
    assert "Validate" in queue.summary
