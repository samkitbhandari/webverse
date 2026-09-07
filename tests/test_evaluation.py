"""The evaluation is itself under test.

A scorecard that quietly degrades is worse than none: it is a claim you keep
making after it stopped being true. These lock in the floors reported in the
README, so a regression in extraction or reasoning fails the build rather than
silently lowering the number on the slide.

Thresholds sit a little below the measured values -- they are regression
guards, not targets to tune against.
"""
from __future__ import annotations

import pytest
import yaml

from evaluation.harness import GOLD, run, values_match


@pytest.fixture(scope="module")
def scorecard():
    return run()


@pytest.fixture(scope="module")
def gold():
    return yaml.safe_load(GOLD.read_text("utf-8"))


def by_name(scorecard, name):
    return next(s for s in scorecard["scores"] if s.name == name)


# ---------------------------------------------------------------------------
# Value comparison must be semantic, or the whole scorecard is meaningless
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "expected,actual",
    [
        ("50000 units per month", "50,000 unit/month"),
        ("70 degC", "70 degC"),
        ("14 days", "14 days"),
        ("480 crore", "4,800,000,000"),
        ("99.3 percent", "99.3 %"),
    ],
)
def test_equivalent_values_are_scored_as_matching(expected, actual):
    assert values_match(expected, actual)


@pytest.mark.parametrize(
    "expected,actual",
    [
        ("70 degC", "60 degC"),
        ("50000 units per month", "31,000 unit/month"),
        ("70 degC", "70 ms"),
        ("14 days", None),
    ],
)
def test_different_values_are_not_scored_as_matching(expected, actual):
    assert not values_match(expected, actual)


# ---------------------------------------------------------------------------
# Regression floors for the published scorecard
# ---------------------------------------------------------------------------
def test_entity_recognition_floor(scorecard):
    assert by_name(scorecard, "entity recognition").f1 >= 0.95


def test_assertion_extraction_floor(scorecard):
    assert by_name(scorecard, "assertion extraction").f1 >= 0.85


def test_supersession_is_exact(scorecard):
    """The regulation must retire the limit it replaces. No partial credit."""
    assert by_name(scorecard, "temporal supersession").f1 == 1.0


def test_contradiction_detection_is_exact(scorecard):
    """Both directions matter: catch the real violation, raise nothing else.

    Precision regressed to 25% once when two measurements of the same property
    were treated as disagreeing. A recall-only assertion would have passed.
    """
    score = by_name(scorecard, "contradiction detection")
    assert score.recall == 1.0
    assert score.precision == 1.0, f"false alarms: {score.spurious}"


def test_decision_impact_is_exact(scorecard):
    """The flagship claim: every affected decision, and nothing else."""
    score = by_name(scorecard, "decision impact")
    assert score.recall == 1.0, f"missed: {score.misses}"
    assert score.precision == 1.0, f"wrongly flagged: {score.spurious}"


def test_every_affected_decision_carries_its_causal_path(scorecard):
    """An unexplained severity score is not evidence."""
    assert scorecard["affected"]
    for decision in scorecard["affected"]:
        assert decision.explanation
        assert decision.hops, "no traversal recorded"
        assert "SUPERSEDES" in decision.explanation or "CONSTRAINS" in decision.explanation


# ---------------------------------------------------------------------------
# The comparison that answers "why not just RAG?"
# ---------------------------------------------------------------------------
def test_retrieval_baseline_cannot_close_the_gap(gold, scorecard):
    """Same chunks, same embedder, generous top-10, scored in its favour."""
    from pathlib import Path

    from evaluation.baseline import evaluate_baseline
    from evaluation.harness import ROOT

    corpus = [ROOT / d["path"] for d in gold["documents"]]
    expected = gold["reasoning"]["expected_affected_decisions"]
    base = evaluate_baseline(corpus, gold["reasoning"]["question"], expected)

    # Retrieval is genuinely good at finding the regulation -- say so.
    assert base["regulation_retrieved"], "a fair baseline must at least retrieve"

    nexus = by_name(scorecard, "decision impact").recall
    assert nexus > base["recall"], (
        f"NEXUS {nexus:.0%} must beat retrieval {base['recall']:.0%} on the "
        "decision-identification task, or the premise of the project is wrong"
    )
    # The qualitative gap matters more than the recall gap.
    assert not base["can_explain_why"]
    assert not base["can_traverse_dependencies"]
