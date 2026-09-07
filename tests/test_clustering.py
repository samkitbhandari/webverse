"""Embedding clustering and its projection into the graph.

The load-bearing property is not that clusters exist -- it is that they exist
*without* corrupting the reasoning that runs over real relationships.
"""
from __future__ import annotations

import pytest

from backend.core.models import EdgeType, NodeType
from backend.core.units import parse_value
from backend.pipeline import Pipeline
from backend.reasoning.impact import propagate
from backend.semantic.clustering import (
    build_concepts, clear_concepts, concept_summary, find_clusters, label_for,
)
from backend.semantic.rules import extract, find_entities

CORPUS = """\
# Supply and Thermal Notes

Volta Cells Ltd capacity is 50000 units per month.
Volta Cells Ltd lead time is 14 days.
Volta Cells Ltd unit cost is 3120 INR.

Battery Pack BP-7 must remain below 60 degC during continuous discharge.
Measured Battery Pack BP-7 max operating temperature reached 68 degC.
Battery Pack BP-7 energy density is 178 Wh.
"""


@pytest.fixture
def world(store, index, tmp_path):
    pipe = Pipeline(store=store, index=index, project_vault=False)
    src = tmp_path / "notes.md"
    src.write_text(CORPUS, encoding="utf-8")
    pipe.ingest_file(src)
    return store


# ---------------------------------------------------------------------------
# Extraction hygiene that clustering depends on
# ---------------------------------------------------------------------------
def test_currency_codes_are_not_entities():
    """"3120 INR" once made INR an entity, then claims were attributed to it."""
    assert "INR" not in find_entities("Volta Cells Ltd unit cost is 3120 INR.")
    ex = extract("Volta Cells Ltd unit cost is 3120 INR.",
                 known_entities=["Volta Cells Ltd"])
    assert all(c.subject != "INR" for c in ex.claims)
    assert any(c.subject == "Volta Cells Ltd" and c.predicate == "cost"
               for c in ex.claims)


def test_written_currency_and_percent_are_parsed():
    assert parse_value("3120", "INR").number == 3120
    assert parse_value("99.3", "percent").unit == "%"


@pytest.mark.parametrize(
    "sentence,expected",
    [
        ("Type Certification is suspended pending review.", False),
        ("Thermal Architecture A7 is no longer compliant.", False),
        ("Thermal Architecture A7 is non-compliant.", False),
        ("Volta Cells Ltd remains certified.", True),
        ("Charging Hub North is now deprecated.", False),
    ],
)
def test_polarity_claims_carry_the_right_boolean(sentence, expected):
    """Negation must invert; otherwise a lapse reads as an endorsement."""
    ex = extract(sentence, known_entities=[
        "Type Certification", "Thermal Architecture A7", "Volta Cells Ltd",
        "Charging Hub North",
    ])
    assert ex.claims, f"no claim extracted from {sentence!r}"
    assert parse_value(ex.claims[0].value).boolean is expected


# ---------------------------------------------------------------------------
# Clustering
# ---------------------------------------------------------------------------
def test_clusters_group_related_beliefs(world):
    clusters = find_clusters(world, threshold=0.62)
    assert clusters
    labels = [c.label.lower() for c in clusters]
    assert any("volta" in l for l in labels) or any(
        m.subject == "Volta Cells Ltd" for c in clusters for m in c.members
    )
    for c in clusters:
        assert c.size >= 2
        assert 0.0 <= c.cohesion <= 1.0


def test_tighter_threshold_never_yields_larger_clusters(world):
    tight = find_clusters(world, threshold=0.40)
    loose = find_clusters(world, threshold=0.85)
    biggest = lambda cs: max((c.size for c in cs), default=0)  # noqa: E731
    assert biggest(tight) <= biggest(loose)


def test_a_cluster_is_named_after_what_members_share(world):
    nodes = [n for n in world.nodes.values() if n.subject == "Volta Cells Ltd"]
    if len(nodes) >= 2:
        assert "Volta Cells Ltd" in label_for(nodes)


def test_build_concepts_writes_nodes_and_membership_edges(world):
    result = build_concepts(world, threshold=0.62)
    assert result["concepts"]

    concepts = world.of_type(NodeType.CONCEPT)
    assert len(concepts) == len(result["concepts"])
    for concept in concepts:
        members = [e for e in world.in_edges(concept.id)
                   if e.type is EdgeType.MEMBER_OF]
        assert len(members) >= 2
        assert concept.attributes["derived"] is True


def test_rebuilding_replaces_rather_than_accumulates(world):
    first = build_concepts(world, threshold=0.62)
    count = len(world.of_type(NodeType.CONCEPT))
    second = build_concepts(world, threshold=0.62)
    assert second["replaced"] == count
    assert len(world.of_type(NodeType.CONCEPT)) == len(second["concepts"])
    assert len(first["concepts"]) == len(second["concepts"])


def test_concepts_can_be_dropped_without_touching_asserted_knowledge(world):
    asserted = {n.id for n in world.nodes.values() if n.type is not NodeType.CONCEPT}
    build_concepts(world, threshold=0.62)
    clear_concepts(world)
    assert world.of_type(NodeType.CONCEPT) == []
    assert {n.id for n in world.nodes.values()} == asserted
    assert not any(e.type is EdgeType.MEMBER_OF for e in world.edges.values())


def test_clustering_does_not_hijack_impact_propagation(world):
    """The point of the low MEMBER_OF influence.

    A concept touching many claims is a hub. If membership propagated like a
    dependency, every claim would reach every other in two hops and severity
    scores would be meaningless.
    """
    seed = next(n for n in world.nodes.values()
                if n.type in (NodeType.REQUIREMENT, NodeType.CONSTRAINT))
    before = propagate(world, [seed.id])
    before_scores = {a.id: a.severity for a in before.affected}

    build_concepts(world, threshold=0.62)
    after = propagate(world, [seed.id])

    for nid, score in before_scores.items():
        hit = next((a for a in after.affected if a.id == nid), None)
        assert hit is not None, "clustering must not remove existing reachability"
        assert hit.severity >= score - 1e-6, "clustering must not weaken real paths"

    # anything newly reachable only through a concept must be barely affected
    via_concept = [
        a for a in after.affected
        if a.id not in before_scores and any(h.edge_type == "MEMBER_OF" for h in a.hops)
    ]
    assert all(a.severity < 0.1 for a in via_concept), \
        "concept hubs must not manufacture significant impact"


def test_concept_summary_reports_members(world):
    build_concepts(world, threshold=0.62)
    summary = concept_summary(world)
    assert summary
    assert summary == sorted(summary, key=lambda c: -c["size"])
    for c in summary:
        assert c["size"] == len(c["members"])
