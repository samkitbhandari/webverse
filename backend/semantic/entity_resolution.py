"""Entity resolution.

The question is always the same: is this "Supplier A" the "Supplier A" we
already know? Get it wrong in one direction and the graph fragments into
synonyms that never contradict each other; get it wrong in the other and two
different suppliers merge into one and every claim about them becomes noise.

Four signals are combined, deliberately none of them alone:

* **lexical**      - normalised string similarity; catches "Supplier A" vs
                     "supplier-a" and punctuation drift
* **semantic**     - embedding similarity over the entity's description and
                     the surrounding passage; catches paraphrase
* **contextual**   - do the entities co-occur with the same neighbours in the
                     graph? Two "Phase 2"s in different projects separate here
* **type agreement** - an Organization does not merge with a Component

Above ``entity_match_threshold`` we merge. Between that and
``entity_review_threshold`` we record the candidate but create a new node,
because a wrong merge is much harder to undo than a duplicate.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Iterable, Sequence

from backend.core.config import settings
from backend.core.models import KNode, NodeType
from backend.graph.store import KnowledgeStore, entity_key
from backend.semantic.embeddings import VectorIndex, cosine, get_embedder

_PUNCT = re.compile(r"[^\w\s]")
_LEGAL = re.compile(
    r"\b(ltd|limited|inc|incorporated|llc|plc|gmbh|corp|corporation|co|company|"
    r"pvt|private|the|of|and)\b", re.I
)


def normalise(name: str) -> str:
    s = _PUNCT.sub(" ", (name or "").lower())
    s = _LEGAL.sub(" ", s)
    return " ".join(s.split())


def lexical_similarity(a: str, b: str) -> float:
    """Blend of sequence ratio and token overlap, so word order matters less."""
    na, nb = normalise(a), normalise(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    seq = SequenceMatcher(None, na, nb).ratio()
    ta, tb = set(na.split()), set(nb.split())
    jaccard = len(ta & tb) / len(ta | tb) if (ta | tb) else 0.0
    contained = 1.0 if (ta <= tb or tb <= ta) else 0.0
    return round(max(0.55 * seq + 0.45 * jaccard, 0.85 * contained), 4)


@dataclass
class Candidate:
    node_id: str
    label: str
    lexical: float = 0.0
    semantic: float = 0.0
    contextual: float = 0.0
    type_agreement: float = 0.5
    score: float = 0.0
    reason: str = ""


@dataclass
class Resolution:
    """Outcome of resolving one extracted name."""

    name: str
    matched_id: str | None = None
    score: float = 0.0
    method: str = "new"
    candidates: list[Candidate] = field(default_factory=list)
    needs_review: bool = False

    @property
    def is_match(self) -> bool:
        return self.matched_id is not None


def _type_agreement(existing: KNode, kind: str) -> float:
    declared = (existing.attributes.get("kind") or "").lower()
    incoming = (kind or "").lower()
    if not declared or not incoming:
        return 0.5
    if declared == incoming:
        return 1.0
    # a few families that are compatible rather than identical
    families = [
        {"organization", "organisation", "supplier", "vendor", "company"},
        {"component", "system", "product", "platform"},
        {"project", "programme", "program", "deployment"},
        {"regulation", "standard", "policy"},
    ]
    for fam in families:
        if declared in fam and incoming in fam:
            return 0.85
    return 0.15


def _contextual_overlap(
    store: KnowledgeStore, node_id: str, context_entity_ids: Iterable[str]
) -> float:
    """Shared graph neighbourhood between a candidate and the current passage."""
    ctx = {c for c in context_entity_ids if c and c != node_id}
    if not ctx:
        return 0.0
    neighbours = {
        (e.target if e.source == node_id else e.source)
        for e in store.incident(node_id)
    }
    if not neighbours:
        return 0.0
    overlap = len(neighbours & ctx)
    return round(min(overlap / min(len(ctx), 4), 1.0), 4)


def resolve(
    store: KnowledgeStore,
    name: str,
    *,
    kind: str = "",
    context_text: str = "",
    context_entity_ids: Sequence[str] = (),
    index: VectorIndex | None = None,
    top_k: int = 8,
) -> Resolution:
    """Decide whether ``name`` is an entity we already hold."""
    res = Resolution(name=name)
    if not name or not name.strip():
        return res

    # 1. exact normalised key -- the overwhelmingly common case, do it first
    if hit := store.find_entity(name):
        res.matched_id, res.score, res.method = hit.id, 1.0, "exact"
        return res

    key = normalise(name)
    if key:
        for existing_key, nid in store.entity_keys.items():
            if normalise(existing_key) == key:
                res.matched_id, res.score, res.method = nid, 0.98, "normalised"
                return res

    # 2. gather candidates: every known entity with any lexical signal, plus
    #    whatever the vector index thinks is nearby
    candidates: dict[str, Candidate] = {}
    for existing in store.of_type(NodeType.ENTITY):
        lex = lexical_similarity(name, existing.label)
        for alias in existing.attributes.get("aliases", []) or []:
            lex = max(lex, lexical_similarity(name, str(alias)))
        if lex >= 0.5:
            candidates[existing.id] = Candidate(
                node_id=existing.id, label=existing.label, lexical=lex
            )

    if index is not None:
        probe = f"{name}. {context_text}".strip()[:600]
        for row in index.search(probe, k=top_k, kind="node"):
            nid = row.get("node_id") or ""
            node = store.get(nid)
            if not node or node.type is not NodeType.ENTITY:
                continue
            cand = candidates.setdefault(
                nid, Candidate(node_id=nid, label=node.label,
                               lexical=lexical_similarity(name, node.label))
            )
            cand.semantic = max(cand.semantic, float(row.get("score", 0.0)))

    if not candidates:
        return res

    # 3. score
    embedder = get_embedder()
    probe_vec = None
    for cand in candidates.values():
        node = store.get(cand.node_id)
        if node is None:
            continue
        if cand.semantic == 0.0 and context_text:
            if probe_vec is None:
                probe_vec = embedder.embed([f"{name}. {context_text}"[:600]])[0]
            node_vec = embedder.embed([f"{node.label}. {node.body}"[:600]])[0]
            cand.semantic = max(0.0, cosine(probe_vec, node_vec))
        cand.contextual = _contextual_overlap(store, cand.node_id, context_entity_ids)
        cand.type_agreement = _type_agreement(node, kind)
        cand.score = round(
            0.45 * cand.lexical
            + 0.25 * cand.semantic
            + 0.15 * cand.contextual
            + 0.15 * cand.type_agreement,
            4,
        )
        cand.reason = (
            f"lexical {cand.lexical:.2f}, semantic {cand.semantic:.2f}, "
            f"context {cand.contextual:.2f}, type {cand.type_agreement:.2f}"
        )

    ranked = sorted(candidates.values(), key=lambda c: -c.score)
    res.candidates = ranked[:5]
    best = ranked[0]

    if best.score >= settings.entity_match_threshold:
        res.matched_id, res.score, res.method = best.node_id, best.score, "scored"
    elif best.score >= settings.entity_review_threshold:
        res.score, res.method, res.needs_review = best.score, "candidate", True
    return res
