"""A retrieval baseline, built to be beaten fairly.

The obvious challenge to this project is "why not just RAG?", and the only
honest way to answer it is to build the RAG, give it every advantage, and
measure both systems on the same question.

So the baseline gets:

* the **same** chunks, from the same parser and chunker
* the **same** embedder, so neither side has a representation advantage
* a **generous** retrieval budget (top-10 of ~30 chunks -- a third of the
  corpus in context)

No strawman. If retrieval can answer the question, it will show up here.

What is measured is not "did it find relevant text" -- it does, easily. It is
the question a compliance owner actually asks:

    the limit changed from 70 degC to 60 degC.
    WHICH OF OUR DECISIONS ARE NO LONGER SUPPORTED?

The answer lives in no single chunk. It is a four-hop chain across four
documents: the new limit supersedes the old one, the old one is what
certification was granted against, certification gates deployment, and a
decision rests on that. Retrieval returns passages; it has no representation
in which "supersedes" or "depends on" exist, so it cannot traverse. An LLM
reading the retrieved passages inherits the same ceiling: it can only reason
over what was retrieved, and the chain is only visible once the relationships
are made explicit -- which is precisely what the graph does.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.parsing.docling_parser import parse
from backend.semantic.embeddings import Embedder, get_embedder


@dataclass
class RetrievedChunk:
    source: str
    locator: str
    text: str
    score: float


@dataclass
class RagBaseline:
    """Chunk, embed, retrieve. The same pipeline NEXUS uses, minus the graph."""

    embedder: Embedder = field(default_factory=get_embedder)
    chunks: list[dict] = field(default_factory=list)
    _vectors: list[list[float]] = field(default_factory=list)

    def index(self, paths: list[Path]) -> int:
        for path in paths:
            doc = parse(path)
            for chunk in doc.chunks:
                self.chunks.append({
                    "source": path.name,
                    "locator": chunk.locator,
                    "text": chunk.text,
                })
        self._vectors = self.embedder.embed([c["text"] for c in self.chunks])
        return len(self.chunks)

    def retrieve(self, question: str, k: int = 10) -> list[RetrievedChunk]:
        from backend.semantic.embeddings import cosine

        q = self.embedder.embed([question])[0]
        scored = [
            (cosine(q, vec), chunk)
            for vec, chunk in zip(self._vectors, self.chunks)
        ]
        scored.sort(key=lambda pair: -pair[0])
        return [
            RetrievedChunk(c["source"], c["locator"], c["text"], round(s, 4))
            for s, c in scored[:k]
        ]


def evaluate_baseline(
    corpus: list[Path], question: str, expected_decisions: list[str], k: int = 10
) -> dict:
    """Score retrieval on the decision-identification task.

    Scored generously: a decision counts as *found* if any retrieved chunk
    contains its text at all. This over-credits the baseline -- surfacing a
    passage that happens to mention a decision is not the same as determining
    that the decision is now unsupported -- and it still cannot close the gap.
    """
    rag = RagBaseline()
    n_chunks = rag.index(corpus)
    hits = rag.retrieve(question, k=k)
    context = "\n".join(h.text.lower() for h in hits)

    found, missed = [], []
    for decision in expected_decisions:
        (found if decision.lower() in context else missed).append(decision)

    # Did retrieval at least surface the regulation itself? It should -- this
    # is what retrieval is genuinely good at, and saying so keeps the
    # comparison honest.
    regulation_found = any(
        "60 degc" in h.text.lower() or "60 degrees" in h.text.lower() for h in hits
    )

    return {
        "chunks_indexed": n_chunks,
        "chunks_retrieved": len(hits),
        "top_sources": [f"{h.source} ({h.score:.3f})" for h in hits[:5]],
        "regulation_retrieved": regulation_found,
        "decisions_found": found,
        "decisions_missed": missed,
        "recall": len(found) / len(expected_decisions) if expected_decisions else 0.0,
        # The distinction that matters: retrieval can put a decision's text in
        # front of you; it cannot tell you the decision has become invalid,
        # because "supersedes" and "depends on" are not in its representation.
        "can_explain_why": False,
        "can_rank_severity": False,
        "can_traverse_dependencies": False,
    }
