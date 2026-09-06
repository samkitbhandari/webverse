"""Semantic change detection.

A text diff tells you a line moved. A semantic diff tells you the world moved.

    OLD: "The system supports 10,000 concurrent users."
    NEW: "The system supports 5,000 concurrent users."

    text diff      -> one line changed
    semantic diff  -> System / capacity: 10,000 -> 5,000 users (-50%)

The second form is the one the impact engine can act on, because it names the
entity, the property, both values and the magnitude of the move. This module
produces that, at two levels: chunk-level (which parts of a source actually
changed, so unchanged parts are never re-extracted) and claim-level (what the
change means for the graph).
"""
from __future__ import annotations

import enum
from typing import Iterable, Sequence

from pydantic import BaseModel, Field

from backend.core.ids import fingerprint
from backend.core.models import KNode, Quantity, SemanticEventType
from backend.core.units import parse_value, relative_delta, same_unit_family


class ChangeKind(str, enum.Enum):
    NEW = "NEW"                    # nothing asserted this property before
    UNCHANGED = "UNCHANGED"
    VALUE_CHANGED = "VALUE_CHANGED"
    UNIT_CHANGED = "UNIT_CHANGED"
    POLARITY_FLIPPED = "POLARITY_FLIPPED"   # supported -> unsupported
    TEXT_REFINED = "TEXT_REFINED"           # same value, different wording


class SemanticChange(BaseModel):
    kind: ChangeKind
    subject: str
    predicate: str
    old_value: str | None = None
    new_value: str | None = None
    old_node_id: str | None = None
    delta: float | None = None          # signed fractional change
    magnitude: float = 0.0              # 0..1, how big a disturbance this is
    direction: str = ""                 # "increase" | "decrease" | ""
    description: str = ""

    def event_type(self) -> SemanticEventType:
        if self.kind is ChangeKind.NEW:
            return SemanticEventType.CLAIM_ADDED
        return SemanticEventType.CLAIM_CHANGED

    @property
    def is_material(self) -> bool:
        return self.kind not in (ChangeKind.UNCHANGED, ChangeKind.TEXT_REFINED)


def _magnitude(delta: float | None, kind: ChangeKind) -> float:
    """Map a change onto 0..1 so the impact engine can scale its seed.

    A 5% drift should not shake the graph as hard as a halving. The curve
    saturates: beyond a doubling or a total collapse, "very large" is as much
    resolution as the downstream reasoning needs.
    """
    if kind is ChangeKind.POLARITY_FLIPPED:
        return 1.0
    if kind is ChangeKind.NEW:
        return 0.6
    if kind is ChangeKind.UNIT_CHANGED:
        return 0.8
    if delta is None:
        return 0.5
    a = abs(delta)
    return round(min(a / (a + 0.35), 1.0), 4)


def compare_values(old: Quantity | None, new: Quantity | None) -> tuple[ChangeKind, float | None]:
    if old is None and new is None:
        return ChangeKind.UNCHANGED, None
    if old is None:
        return ChangeKind.NEW, None
    if new is None:
        return ChangeKind.UNCHANGED, None

    if old.boolean is not None and new.boolean is not None:
        if old.boolean != new.boolean:
            return ChangeKind.POLARITY_FLIPPED, None
        return ChangeKind.UNCHANGED, None

    if old.is_numeric and new.is_numeric:
        if not same_unit_family(old, new):
            return ChangeKind.UNIT_CHANGED, None
        delta = relative_delta(old, new)
        if delta is None or abs(delta) < 1e-9:
            return ChangeKind.UNCHANGED, 0.0
        return ChangeKind.VALUE_CHANGED, delta

    a = (old.text or old.raw or "").strip().lower()
    b = (new.text or new.raw or "").strip().lower()
    if a == b:
        return ChangeKind.UNCHANGED, None
    return ChangeKind.TEXT_REFINED if a and b else ChangeKind.VALUE_CHANGED, None


def diff_claim(previous: KNode | None, subject: str, predicate: str,
               new_value: Quantity | None) -> SemanticChange:
    """Interpret a newly asserted value against what the graph already held."""
    if previous is None:
        return SemanticChange(
            kind=ChangeKind.NEW,
            subject=subject,
            predicate=predicate,
            new_value=new_value.render() if new_value else None,
            magnitude=_magnitude(None, ChangeKind.NEW),
            description=(
                f"{subject} / {predicate}: first assertion "
                f"({new_value.render() if new_value else 'no value'})"
            ),
        )

    kind, delta = compare_values(previous.value, new_value)
    direction = ""
    if delta is not None and delta != 0:
        direction = "increase" if delta > 0 else "decrease"

    old_r = previous.value.render() if previous.value else None
    new_r = new_value.render() if new_value else None
    if kind is ChangeKind.UNCHANGED:
        desc = f"{subject} / {predicate}: unchanged at {new_r}"
    elif kind is ChangeKind.POLARITY_FLIPPED:
        desc = f"{subject} / {predicate}: {old_r} -> {new_r} (polarity flipped)"
    elif delta is not None:
        desc = f"{subject} / {predicate}: {old_r} -> {new_r} ({delta:+.1%})"
    else:
        desc = f"{subject} / {predicate}: {old_r} -> {new_r}"

    return SemanticChange(
        kind=kind,
        subject=subject,
        predicate=predicate,
        old_value=old_r,
        new_value=new_r,
        old_node_id=previous.id,
        delta=delta,
        magnitude=_magnitude(delta, kind),
        direction=direction,
        description=desc,
    )


# ---------------------------------------------------------------------------
# Chunk-level diff
# ---------------------------------------------------------------------------
class ChunkDiff(BaseModel):
    added: list[int] = Field(default_factory=list)
    removed: list[str] = Field(default_factory=list)     # fingerprints
    unchanged: list[int] = Field(default_factory=list)

    @property
    def has_changes(self) -> bool:
        return bool(self.added or self.removed)


def diff_chunks(old_fingerprints: Sequence[str], new_texts: Sequence[str]) -> ChunkDiff:
    """Which chunks of a re-ingested source are actually new.

    Re-extracting an entire 40-page document because one paragraph moved is
    both slow and expensive when a provider is in the loop, so unchanged chunks
    are skipped outright.
    """
    old = set(old_fingerprints)
    diff = ChunkDiff()
    seen: set[str] = set()
    for i, text in enumerate(new_texts):
        fp = fingerprint(text)
        seen.add(fp)
        (diff.unchanged if fp in old else diff.added).append(i)
    diff.removed = sorted(old - seen)
    return diff


def describe_changes(changes: Iterable[SemanticChange]) -> str:
    """One-paragraph human summary of a batch of semantic changes."""
    material = [c for c in changes if c.is_material]
    if not material:
        return "No semantic change: the source was re-read but asserts the same knowledge."
    lines = [f"{len(material)} semantic change(s) detected:"]
    for c in sorted(material, key=lambda c: -c.magnitude)[:12]:
        lines.append(f"  - [{c.kind.value}] {c.description}")
    return "\n".join(lines)
