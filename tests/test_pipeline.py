"""End-to-end ingestion: raw prose in, reasoned graph out."""
from __future__ import annotations

import pytest

from backend.core.models import EpistemicStatus, NodeType
from backend.counterfactual import simulator
from backend.counterfactual.simulator import Intervention, InterventionType
from backend.pipeline import Pipeline
from backend.reasoning import temporal
from backend.reasoning.contradiction import ContradictionKind, detect_all
from backend.semantic.rules import extract
from backend.semantic.semantic_diff import ChangeKind, diff_chunks

SPEC = """\
# Battery Thermal Design Specification

## Operating envelope

Battery Pack BP-7 must remain below 70 degC during continuous discharge.
Thermal Architecture A7 implements the cooling approach for Battery Pack BP-7.

## Validation

Measured pack temperature reached 68 degC during the summer duty cycle test.

## Supply

Volta Cells Ltd capacity is 50000 units per month.
We have selected Volta Cells Ltd because their lead time is 14 days.
"""

REGULATION = """\
# UNECE R100 Revision 3

## Summary

UNECE R100 rev.3 was published and reduced the battery operating limit from
70 degC to 60 degC for urban passenger service.

## Applicability

Battery Pack BP-7 must remain below 60 degC during continuous discharge.
"""


@pytest.fixture
def pipeline(store, index):
    return Pipeline(store=store, index=index, project_vault=False)


@pytest.fixture
def inbox(tmp_path):
    d = tmp_path / "inbox"
    d.mkdir(exist_ok=True)
    return d


def write(inbox, name: str, text: str):
    p = inbox / name
    p.write_text(text, encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# Rule extraction
# ---------------------------------------------------------------------------
def test_extraction_recovers_subject_predicate_value():
    ex = extract(SPEC, known_entities=["Battery Pack BP-7", "Volta Cells Ltd"])
    constraints = {(c.subject, c.predicate, c.operator) for c in ex.constraints}
    assert ("Battery Pack BP-7", "max operating temperature", "LT") in constraints
    capacity = next(c for c in ex.claims if c.predicate == "capacity")
    assert capacity.subject == "Volta Cells Ltd"
    assert capacity.value == "50000"          # unit is not duplicated into value
    assert capacity.unit == "units per month"


def test_hard_wrapped_sentences_are_not_split_mid_clause():
    """"reduced from 70 to 60" must survive an 80-column line break."""
    ex = extract(REGULATION, known_entities=["Battery Pack BP-7"])
    limits = [c for c in ex.constraints if c.predicate == "max operating temperature"]
    assert limits and limits[0].value == "60"


def test_headings_do_not_become_the_subject():
    ex = extract(SPEC, known_entities=["Battery Pack BP-7"])
    subjects = {c.subject for c in ex.claims} | {c.subject for c in ex.constraints}
    assert "Battery Thermal Design Specification" not in subjects


def test_identifiers_do_not_become_claims():
    ex = extract("Fleet Deployment Phase 2 depends on Thermal Architecture A7.")
    assert not any(c.value in {"2", "7"} for c in ex.claims)


# ---------------------------------------------------------------------------
# Ingestion
# ---------------------------------------------------------------------------
def test_ingest_builds_a_graph_with_provenance(pipeline, inbox, store):
    result = pipeline.ingest_file(write(inbox, "spec.md", SPEC))
    assert result.nodes_added > 0 and result.edges_added > 0

    entity = store.find_entity("Battery Pack BP-7")
    assert entity is not None

    claims = store.of_type(NodeType.CLAIM, NodeType.OBSERVATION)
    assert claims
    assert all(c.provenance and c.provenance[0].source_path for c in claims)
    assert store.of_type(NodeType.EVIDENCE)


def test_reingesting_unchanged_content_is_a_no_op(pipeline, inbox, store):
    path = write(inbox, "spec.md", SPEC)
    pipeline.ingest_file(path)
    before = len(store.nodes)
    again = pipeline.ingest_file(path)
    assert again.skipped_reason
    assert len(store.nodes) == before


def test_entities_are_reused_across_documents(pipeline, inbox, store):
    pipeline.ingest_file(write(inbox, "spec.md", SPEC))
    pipeline.ingest_file(write(inbox, "reg.md", REGULATION))
    matches = [n for n in store.of_type(NodeType.ENTITY)
               if "BP-7" in n.label]
    assert len(matches) == 1, "the same pack must not become two entities"


def test_a_regulation_supersedes_and_exposes_a_violation(pipeline, inbox, store):
    """The scenario the whole system exists for, driven from raw text."""
    pipeline.ingest_file(write(inbox, "spec.md", SPEC))
    result = pipeline.ingest_file(write(inbox, "reg.md", REGULATION))

    tl = temporal.timeline(store, "Battery Pack BP-7", "max operating temperature")
    values = {p.value: p.status for p in tl.points}
    assert any("70 degC" in v for v in values)
    assert any("60 degC" in v for v in values)
    assert EpistemicStatus.SUPERSEDED.value in {s for s in values.values()}

    kinds = {c["kind"] for c in result.contradictions}
    assert ContradictionKind.CONSTRAINT.value in kinds

    assert result.impact is not None and result.impact.total_affected > 0


def test_removing_a_source_weakens_rather_than_deletes(pipeline, inbox, store):
    path = write(inbox, "spec.md", SPEC)
    pipeline.ingest_file(path)
    claim = store.of_type(NodeType.CLAIM)[0]
    before = claim.confidence

    pipeline.retire_source(str(path))
    after = store.get(claim.id)
    assert after is not None, "knowledge is not deleted with its source"
    assert after.confidence < before
    assert after.status is EpistemicStatus.UNVERIFIED


def test_forgetting_a_node_erases_it_everywhere(pipeline, inbox, store, index):
    """Retiring weakens; forgetting removes. This is the destructive one."""
    pipeline.ingest_file(write(inbox, "spec.md", SPEC))
    claim = store.of_type(NodeType.CLAIM)[0]
    edges = len(store.incident(claim.id))

    result = pipeline.forget_node(claim.id)
    assert result["removed"] and result["edges_removed"] == edges
    assert store.get(claim.id) is None
    # gone from durable storage too, not just the working set
    assert not store.query("MATCH (n:KNode) WHERE n.id = $id RETURN n.id",
                           {"id": claim.id})
    # and no dangling relationships were left behind
    assert all(e.source != claim.id and e.target != claim.id
               for e in store.edges.values())


def test_forgetting_an_unknown_node_is_reported_not_raised(pipeline):
    result = pipeline.forget_node("NOPE")
    assert result["removed"] is False and "NOPE" in result["reason"]


def test_purging_a_source_spares_corroborated_knowledge(pipeline, inbox, store):
    """A claim two documents both assert must survive one of them being purged."""
    a = write(inbox, "a.md", "# A\n\nVolta Cells Ltd capacity is 50000 units per month.\n")
    b = write(inbox, "b.md", "# B\n\nVolta Cells Ltd capacity is 50000 units per month.\n")
    pipeline.ingest_file(a)
    pipeline.ingest_file(b)

    claim = next(n for n in store.of_type(NodeType.CLAIM) if n.predicate == "capacity")
    assert len({p.source_path for p in claim.provenance}) == 2, "should be corroborated"

    result = pipeline.purge_source(str(a))
    survivor = store.get(claim.id)
    assert survivor is not None, "corroborated knowledge must not be purged"
    assert claim.id in result["kept_with_other_evidence"]
    assert {p.source_path for p in survivor.provenance} == {str(b)}


def test_purging_removes_knowledge_with_no_other_evidence(pipeline, inbox, store):
    pipeline.ingest_file(write(inbox, "spec.md", SPEC))
    only = write(inbox, "only.md", "# Only\n\nAcme Corp capacity is 900 units per month.\n")
    pipeline.ingest_file(only)

    before = len(store.nodes)
    result = pipeline.purge_source(str(only))
    assert result["removed"]
    assert len(store.nodes) < before
    assert not any(
        p.source_path == str(only)
        for n in store.nodes.values() for p in n.provenance
    )


def test_chunk_diff_skips_unchanged_text():
    old = ["alpha text here", "beta text here"]
    from backend.core.ids import fingerprint

    diff = diff_chunks([fingerprint(t) for t in old],
                       ["alpha text here", "gamma text here"])
    assert diff.added == [1] and diff.unchanged == [0] and diff.removed


# ---------------------------------------------------------------------------
# Counterfactual
# ---------------------------------------------------------------------------
def test_simulation_never_touches_the_real_graph(pipeline, inbox, store):
    pipeline.ingest_file(write(inbox, "spec.md", SPEC))
    target = store.of_type(NodeType.ENTITY)[0]
    before = {n.id: n.status for n in store.nodes.values()}

    simulator.simulate(
        store,
        [Intervention(type=InterventionType.INVALIDATE, target=target.id)],
        name="destructive",
    )
    after = {n.id: n.status for n in store.nodes.values()}
    assert before == after


def test_simulation_reports_a_trade_not_a_score(pipeline, inbox, store):
    pipeline.ingest_file(write(inbox, "spec.md", SPEC))
    arch = store.find_entity("Thermal Architecture A7")
    result = simulator.simulate(
        store,
        [Intervention(type=InterventionType.INVALIDATE, target=arch.id)],
        name="withdraw A7",
    )
    assert result.verdict
    assert {d.direction for d in result.deltas} & {"better", "worse", "unchanged"}
    assert result.baseline.health_score >= 0


def test_scenarios_are_ranked_with_a_rationale(pipeline, inbox, store):
    pipeline.ingest_file(write(inbox, "spec.md", SPEC))
    entities = store.of_type(NodeType.ENTITY)
    results = [
        simulator.simulate(
            store, [Intervention(type=InterventionType.INVALIDATE, target=e.id)],
            name=f"drop {e.label}",
        )
        for e in entities[:2]
    ]
    comparison = simulator.compare(results)
    assert comparison["recommended"] in {r.scenario for r in results}
    assert comparison["rationale"]
    scores = [r["score"] for r in comparison["ranking"]]
    assert scores == sorted(scores, reverse=True)


# ---------------------------------------------------------------------------
# Vault projection
# ---------------------------------------------------------------------------
def test_vault_projection_is_readable_and_idempotent(pipeline, inbox, store, tmp_path):
    from backend.vault.markdown_writer import project_all

    pipeline.ingest_file(write(inbox, "spec.md", SPEC))
    vault = tmp_path / "vault"

    first = project_all(store, root=vault)
    second = project_all(store, root=vault)
    assert first == second > 0

    notes = list(vault.rglob("*.md"))
    assert (vault / "README.md").exists()
    body = next(p for p in notes if p.name != "README.md").read_text("utf-8")
    assert body.startswith("---")           # YAML frontmatter
    assert "id:" in body and "status:" in body
    assert "[[" in body or "## Status" in body
