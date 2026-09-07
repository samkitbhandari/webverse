"""Evaluation harness.

"How do you know it works?" is the question every review asks, and a passing
unit-test suite is not an answer -- it proves the code runs, not that the
knowledge it produces is correct.

This scores NEXUS against :file:`gold_standard.yaml`, a set of labels written
by reading the documents rather than by recording what the system currently
outputs. That asymmetry matters: a circular gold set scores 100% and proves
nothing, so these labels deliberately include things the extractor may miss.
The gaps are the finding.

Four tasks are measured, in increasing order of what they demonstrate:

1. **Entity recognition** - did we find the things being talked about?
2. **Assertion extraction** - did we recover (subject, predicate, value)?
   Values are compared *semantically*: "50000 units per month" and
   "50,000 unit/month" are the same answer because both normalise to the same
   number and canonical unit.
3. **Reasoning** - supersession and contradiction detection after a change.
4. **Decision impact** - which decisions a regulatory change invalidates.
   Task 4 is the one retrieval cannot do, and it is scored with both recall
   *and* a false-positive check, because an engine that marks everything
   downstream would otherwise look perfect.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.core.models import NodeType
from backend.core.units import parse_value
from backend.demo.seed import apply_curated_links
from backend.graph.store import KnowledgeStore
from backend.pipeline import Pipeline
from backend.reasoning.contradiction import detect_all
from backend.reasoning.impact import propagate
from backend.semantic.embeddings import VectorIndex

GOLD = Path(__file__).parent / "gold_standard.yaml"


# ---------------------------------------------------------------------------
# Scoring primitives
# ---------------------------------------------------------------------------
@dataclass
class Score:
    name: str
    tp: int = 0
    fp: int = 0
    fn: int = 0
    misses: list[str] = field(default_factory=list)
    spurious: list[str] = field(default_factory=list)

    @property
    def precision(self) -> float:
        return self.tp / (self.tp + self.fp) if (self.tp + self.fp) else 0.0

    @property
    def recall(self) -> float:
        return self.tp / (self.tp + self.fn) if (self.tp + self.fn) else 0.0

    @property
    def f1(self) -> float:
        p, r = self.precision, self.recall
        return 2 * p * r / (p + r) if (p + r) else 0.0

    def row(self) -> str:
        return (f"  {self.name:<26} P {self.precision:5.1%}   R {self.recall:5.1%}   "
                f"F1 {self.f1:5.1%}    ({self.tp} correct, {self.fn} missed, "
                f"{self.fp} spurious)")


def _norm(text: str) -> str:
    return " ".join(str(text or "").strip().lower().split())


def values_match(expected: str, actual: str | None) -> bool:
    """Compare two values the way a human would, not textually.

    Both sides go through the same normalisation the graph uses, so a match
    means "these denote the same quantity", not "these strings are equal".
    """
    if actual is None:
        return False
    a, b = parse_value(expected), parse_value(actual)
    if a.is_numeric and b.is_numeric:
        if a.unit and b.unit and a.unit != b.unit:
            return False
        if a.number == 0:
            return b.number == 0
        return abs(a.number - b.number) / abs(a.number) < 0.01
    if a.boolean is not None or b.boolean is not None:
        return a.boolean == b.boolean
    return _norm(expected) == _norm(actual)


# ---------------------------------------------------------------------------
# The run
# ---------------------------------------------------------------------------
def build_world(tmp: Path, docs: Iterable[str]) -> tuple[KnowledgeStore, Pipeline]:
    store = KnowledgeStore(kuzu_path=tmp / "kz")
    pipe = Pipeline(store=store, index=VectorIndex(path=tmp / "lance"),
                    project_vault=False)
    for rel in docs:
        pipe.ingest_file(ROOT / rel)
    apply_curated_links(store)
    return store, pipe


def score_entities(store: KnowledgeStore, gold: dict) -> Score:
    s = Score("entity recognition")
    expected: set[str] = set()
    for doc in gold["documents"]:
        expected |= {_norm(e) for e in doc.get("entities", [])}

    found = {_norm(n.label) for n in store.of_type(NodeType.ENTITY)}
    for want in expected:
        if any(want == f or want in f or f in want for f in found):
            s.tp += 1
        else:
            s.fn += 1
            s.misses.append(want)
    # An entity the graph invented that no document names is noise.
    for f in found:
        if not any(want == f or want in f or f in want for want in expected):
            s.fp += 1
            s.spurious.append(f)
    return s


def score_assertions(store: KnowledgeStore, gold: dict) -> Score:
    s = Score("assertion extraction")
    nodes = [n for n in store.nodes.values() if n.subject and n.predicate]

    matched_ids: set[str] = set()
    for doc in gold["documents"]:
        for want in doc.get("assertions", []):
            hit = None
            for n in nodes:
                if _norm(n.subject) != _norm(want["subject"]):
                    continue
                if _norm(n.predicate) != _norm(want["predicate"]):
                    continue
                if not values_match(want["value"], n.value.render() if n.value else None):
                    continue
                hit = n
                break
            if hit is not None:
                s.tp += 1
                matched_ids.add(hit.id)
            else:
                s.fn += 1
                s.misses.append(
                    f"{want['subject']} / {want['predicate']} = {want['value']}")

    # Extracted assertions nobody asked for. Counted, but listed separately
    # because some are legitimate detail the labels simply do not cover.
    for n in nodes:
        if n.id not in matched_ids and n.type is not NodeType.CONCEPT:
            s.fp += 1
            s.spurious.append(
                f"{n.subject} / {n.predicate} = "
                f"{n.value.render() if n.value else '-'}")
    return s


def score_reasoning(store: KnowledgeStore, gold: dict) -> tuple[Score, Score]:
    """Supersession and contradiction detection after the trigger document."""
    spec = gold["reasoning"]

    sup = Score("temporal supersession")
    for want in spec.get("expected_supersessions", []):
        siblings = store.siblings_of_triple(want["subject"], want["predicate"])
        old = next(
            (n for n in siblings
             if values_match(want["old_value"], n.value.render() if n.value else None)),
            None,
        )
        new = next(
            (n for n in siblings
             if values_match(want["new_value"], n.value.render() if n.value else None)),
            None,
        )
        if old is not None and new is not None and old.status.value == "SUPERSEDED":
            sup.tp += 1
        else:
            sup.fn += 1
            sup.misses.append(
                f"{want['subject']} / {want['predicate']}: "
                f"{want['old_value']} -> {want['new_value']}"
                f" (old status: {old.status.value if old else 'not found'})")

    con = Score("contradiction detection")
    found = detect_all(store)
    for want in spec.get("expected_contradictions", []):
        hit = any(
            c.kind.value == want["kind"]
            and _norm(c.subject) == _norm(want["subject"])
            and _norm(c.predicate) == _norm(want["predicate"])
            for c in found
        )
        if hit:
            con.tp += 1
        else:
            con.fn += 1
            con.misses.append(f"{want['kind']} on {want['subject']} / {want['predicate']}")
    # Contradictions raised beyond those labelled are counted as false alarms,
    # and named -- an unlisted one is sometimes a genuine find the labels
    # missed rather than an error, and that is only visible if it is shown.
    for c in found:
        labelled = any(
            c.kind.value == want["kind"]
            and _norm(c.subject) == _norm(want["subject"])
            and _norm(c.predicate) == _norm(want["predicate"])
            for want in spec.get("expected_contradictions", [])
        )
        if not labelled:
            con.fp += 1
            con.spurious.append(f"[{c.kind.value}] {c.description}")
    return sup, con


def score_impact(store: KnowledgeStore, gold: dict, seeds: list[str]) -> tuple[Score, list]:
    """The task retrieval cannot do: which decisions did this change break?"""
    spec = gold["reasoning"]
    s = Score("decision impact")

    report = propagate(store, {sid: 1.0 for sid in seeds}) if seeds else None
    affected = report.affected_decisions if report else []
    affected_labels = [_norm(a.label) for a in affected]

    for want in spec.get("expected_affected_decisions", []):
        if any(_norm(want) in lbl for lbl in affected_labels):
            s.tp += 1
        else:
            s.fn += 1
            s.misses.append(want)

    for never in spec.get("expected_unaffected_decisions", []):
        if any(_norm(never) in lbl for lbl in affected_labels):
            s.fp += 1
            s.spurious.append(f"{never} (should not be affected)")

    return s, affected


def run(verbose: bool = False) -> dict[str, Any]:
    import shutil
    import tempfile

    gold = yaml.safe_load(GOLD.read_text("utf-8"))
    trigger = gold["reasoning"]["trigger"]
    baseline_docs = [d["path"] for d in gold["documents"] if d["path"] != trigger]

    tmp = Path(tempfile.mkdtemp())
    try:
        store, pipe = build_world(tmp, baseline_docs)
        before_decisions = len(store.of_type(NodeType.DECISION))

        # The trigger is ingested before extraction is scored, because the gold
        # set labels every document including the trigger. Scoring extraction
        # on the pre-trigger world would count the regulation's own assertion
        # as a miss and understate the extractor.
        result = pipe.ingest_file(ROOT / trigger)
        seeds = list(result.impact.seeds) if result.impact else []

        entities = score_entities(store, gold)
        assertions = score_assertions(store, gold)

        supersession, contradiction = score_reasoning(store, gold)
        impact, affected = score_impact(store, gold, seeds)

        scores = [entities, assertions, supersession, contradiction, impact]
        return {
            "scores": scores,
            "affected": affected,
            "decisions_total": before_decisions,
            "extractor": result.extractor or "rules",
            "nodes": len(store.nodes),
            "edges": len(store.edges),
            "store": None,
        }
    finally:
        try:
            store.close()
        except Exception:
            pass
        shutil.rmtree(tmp, ignore_errors=True)
