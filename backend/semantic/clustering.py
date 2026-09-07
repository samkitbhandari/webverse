"""Semantic clustering: turning embeddings into graph structure.

Chunks are embedded on the way in, which makes *retrieval* possible. This goes
one step further and asks what the embedding space says about the knowledge as
a whole: which beliefs are talking about the same thing, even when no document
ever links them.

Those groups become ``Concept`` nodes. They are emergent -- no source asserts
them -- so they are treated as derived structure throughout: rebuilt from
scratch on demand, never depended upon, and given deliberately weak edges so
they cannot distort the reasoning that runs over real relationships.

Three choices worth stating:

**Average linkage, not connected components.** Thresholding a similarity graph
and taking connected components chains: A resembles B, B resembles C, so A and
C land together even when they are unrelated. On a corpus that shares
vocabulary this collapses into one giant blob. Average linkage compares a
candidate against a cluster's mean distance, which resists chaining.

**Membership edges are nearly inert.** A concept touching twenty claims is a
hub. If membership propagated impact normally, every claim would reach every
other claim in two hops and impact analysis would become meaningless. The edge
carries influence 0.08 (see ``EDGE_INFLUENCE``), enough to show the association
in the UI and not enough to move a severity score.

**Rebuilds replace, never accumulate.** Concepts are keyed by content, and a
rebuild deletes the previous generation first, so running it repeatedly is
idempotent rather than a slow leak of near-duplicate topics.
"""
from __future__ import annotations

import logging
import re
from collections import Counter
from dataclasses import dataclass, field

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
from backend.graph.store import KnowledgeStore
from backend.semantic.embeddings import Embedder, get_embedder

log = logging.getLogger("nexus.clustering")

#: Node types worth clustering. Evidence is excluded -- grouping documents by
#: how similar their prose is tells you about writing style, not knowledge.
CLUSTERABLE = (
    NodeType.CLAIM, NodeType.OBSERVATION, NodeType.ASSUMPTION,
    NodeType.REQUIREMENT, NodeType.CONSTRAINT, NodeType.RISK,
    NodeType.DECISION, NodeType.EVENT, NodeType.ENTITY,
)

_STOP = {
    "the", "a", "an", "of", "for", "to", "is", "are", "was", "were", "be",
    "must", "shall", "will", "has", "have", "had", "its", "their", "this",
    "that", "at", "in", "on", "by", "with", "and", "or", "not", "per", "we",
    "it", "each", "any", "all", "may", "can", "should", "there", "because",
    "than", "from", "during", "across", "under", "over", "if", "no", "our",
    "remain", "reached", "following", "current", "revised", "new",
}
_WORD = re.compile(r"[a-z][a-z0-9-]{2,}")


@dataclass
class Cluster:
    members: list[KNode] = field(default_factory=list)
    label: str = ""
    cohesion: float = 0.0        # mean pairwise similarity, 0..1

    @property
    def size(self) -> int:
        return len(self.members)


def node_text(node: KNode) -> str:
    """What a node 'says', for embedding purposes."""
    bits = [node.label]
    if node.subject and node.predicate:
        bits.append(f"{node.subject} {node.predicate}")
    if node.body:
        bits.append(node.body[:400])
    return ". ".join(b for b in bits if b)[:800]


def label_for(members: list[KNode]) -> str:
    """Name a cluster after what its members have in common.

    A shared subject is the strongest signal and is used verbatim when every
    member agrees. Otherwise the most frequent distinctive words across the
    members' labels stand in -- crude, but it beats "Cluster 7".
    """
    subjects = {m.subject.strip() for m in members if m.subject}
    if len(subjects) == 1:
        subject = subjects.pop()
        predicates = {m.predicate for m in members if m.predicate}
        if len(predicates) == 1:
            return f"{subject}: {predicates.pop()}"
        return subject

    counts: Counter[str] = Counter()
    for m in members:
        for word in set(_WORD.findall(m.label.lower())):
            if word not in _STOP:
                counts[word] += 1
    top = [w for w, n in counts.most_common(3) if n > 1]
    if not top:
        top = [w for w, _ in counts.most_common(2)]
    return " / ".join(top).title() if top else "Unnamed topic"


# ---------------------------------------------------------------------------
# Clustering
# ---------------------------------------------------------------------------
def _cluster_indices(vectors, threshold: float) -> list[int]:
    """Assign each vector a cluster id using average-linkage agglomeration."""
    import numpy as np
    from scipy.cluster.hierarchy import fcluster, linkage
    from scipy.spatial.distance import pdist

    matrix = np.asarray(vectors, dtype=float)
    if matrix.shape[0] < 2:
        return [1] * matrix.shape[0]

    distances = pdist(matrix, metric="cosine")
    # An all-zero or degenerate vector yields nan; treat it as maximally far
    # rather than letting scipy fail on the whole batch.
    distances = np.nan_to_num(distances, nan=1.0, posinf=1.0, neginf=1.0)
    linkage_matrix = linkage(distances, method="average")
    return list(fcluster(linkage_matrix, t=threshold, criterion="distance"))


def _cohesion(vectors, members: list[int]) -> float:
    import numpy as np

    if len(members) < 2:
        return 1.0
    matrix = np.asarray([vectors[i] for i in members], dtype=float)
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    unit = matrix / norms
    sims = unit @ unit.T
    n = len(members)
    return float((sims.sum() - n) / (n * (n - 1))) if n > 1 else 1.0


def find_clusters(
    store: KnowledgeStore,
    *,
    threshold: float = 0.62,
    min_size: int = 2,
    max_size: int = 25,
    embedder: Embedder | None = None,
    types: tuple[NodeType, ...] = CLUSTERABLE,
) -> list[Cluster]:
    """Group live nodes by embedding similarity.

    ``threshold`` is a cosine *distance* ceiling: lower means tighter, more
    numerous clusters. ``max_size`` discards anything that swallowed a large
    fraction of the graph -- a cluster of everything is not a topic.
    """
    nodes = [
        n for n in store.nodes.values()
        if n.type in types and n.status.is_live and n.type is not NodeType.CONCEPT
    ]
    if len(nodes) < min_size:
        return []

    embedder = embedder or get_embedder()
    vectors = embedder.embed([node_text(n) for n in nodes])
    assignments = _cluster_indices(vectors, threshold)

    grouped: dict[int, list[int]] = {}
    for idx, cid in enumerate(assignments):
        grouped.setdefault(int(cid), []).append(idx)

    clusters: list[Cluster] = []
    for indices in grouped.values():
        if not (min_size <= len(indices) <= max_size):
            continue
        members = [nodes[i] for i in indices]
        clusters.append(
            Cluster(
                members=members,
                label=label_for(members),
                cohesion=round(_cohesion(vectors, indices), 4),
            )
        )
    clusters.sort(key=lambda c: (-c.size, -c.cohesion))
    return clusters


# ---------------------------------------------------------------------------
# Projection into the graph
# ---------------------------------------------------------------------------
def clear_concepts(store: KnowledgeStore) -> int:
    """Remove the previous generation of concepts and their membership edges."""
    doomed = [n.id for n in store.of_type(NodeType.CONCEPT)]
    for nid in doomed:
        store.remove_node(nid)
    return len(doomed)


def build_concepts(
    store: KnowledgeStore,
    *,
    threshold: float = 0.62,
    min_size: int = 2,
    max_size: int = 25,
    embedder: Embedder | None = None,
    replace: bool = True,
) -> dict:
    """Cluster the graph and write the result back as Concept nodes."""
    removed = clear_concepts(store) if replace else 0
    clusters = find_clusters(
        store, threshold=threshold, min_size=min_size,
        max_size=max_size, embedder=embedder,
    )

    created: list[dict] = []
    now = utcnow()
    for cluster in clusters:
        member_ids = sorted(m.id for m in cluster.members)
        concept = KNode(
            id=node_id(NodeType.CONCEPT),
            type=NodeType.CONCEPT,
            label=cluster.label,
            body=(
                f"Emergent topic covering {cluster.size} beliefs, discovered by "
                f"embedding similarity (cohesion {cluster.cohesion:.2f}). "
                "Derived structure -- rebuilt on demand, not asserted by any source."
            ),
            status=EpistemicStatus.PROBABLE,
            confidence=round(min(0.4 + cluster.cohesion / 2, 0.9), 3),
            created_at=now,
            valid_from=now,
            fingerprint=fingerprint("concept", *member_ids),
            provenance=[Provenance(
                source_label="embedding clustering",
                authority=0.4, observed_at=now, extractor="clustering",
            )],
            attributes={
                "members": member_ids,
                "cohesion": cluster.cohesion,
                "size": cluster.size,
                "derived": True,
            },
        )
        store.add_node(concept)

        for member in cluster.members:
            store.add_edge(KEdge(
                id=edge_id(),
                source=member.id,
                target=concept.id,
                type=EdgeType.MEMBER_OF,
                weight=round(max(cluster.cohesion, 0.1), 3),
                confidence=0.5,
                created_at=now,
                rationale=f"clustered into '{cluster.label}' by embedding similarity",
                attributes={"derived": True},
            ))

        created.append({
            "id": concept.id,
            "label": concept.label,
            "size": cluster.size,
            "cohesion": cluster.cohesion,
            "members": member_ids,
        })

    log.info("clustering: %d concept(s) over %d member(s); replaced %d",
             len(created), sum(c["size"] for c in created), removed)
    return {
        "concepts": created,
        "replaced": removed,
        "threshold": threshold,
        "clustered_nodes": sum(c["size"] for c in created),
    }


def concept_summary(store: KnowledgeStore) -> list[dict]:
    """Current concepts and what they contain, for the API and UI."""
    out: list[dict] = []
    for concept in sorted(store.of_type(NodeType.CONCEPT), key=lambda n: n.id):
        members = [
            store.get(e.source) for e in store.in_edges(concept.id)
            if e.type is EdgeType.MEMBER_OF
        ]
        out.append({
            "id": concept.id,
            "label": concept.label,
            "cohesion": concept.attributes.get("cohesion"),
            "size": len([m for m in members if m]),
            "members": [
                {"id": m.id, "type": m.type.value, "label": m.label,
                 "status": m.status.value}
                for m in members if m
            ],
        })
    return sorted(out, key=lambda c: -c["size"])
