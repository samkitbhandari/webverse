"""Demo world builder.

Loads the Helios urban-mobility corpus through the real pipeline -- the same
parse/compile/resolve/reconcile path any file dropped into ``incoming_files``
takes -- and then adds a small set of curated structural links.

Why curated links exist, stated plainly: a rule extractor recovers what a
sentence says, and some structure is never written in one sentence. "Thermal
Architecture A7 implements the 70 degC limit" is knowledge an engineer holds
across two documents. Real deployments curate exactly this, and inventing it
silently would misrepresent what the extractor can do -- so the links are
declared here, in one visible list, rather than smuggled into the corpus.

The change documents in ``changes/`` are deliberately NOT seeded: they are the
live demo, and they go through the watcher like anything else.
"""
from __future__ import annotations

import logging
import shutil
from pathlib import Path
from typing import Any

from backend.core.config import settings
from backend.core.ids import edge_id
from backend.core.models import EdgeType, KEdge, KNode, NodeType
from backend.graph.store import KnowledgeStore
from backend.pipeline import Pipeline

log = logging.getLogger("nexus.demo")

HERE = Path(__file__).parent
CORPUS = HERE / "corpus"
CHANGES = HERE / "changes"


#: Structural knowledge that spans documents. Each side is a matcher, not an
#: id, because ids are allocated at ingest time.
CURATED_LINKS: list[dict[str, Any]] = [
    {
        "from": {"entity": "Thermal Architecture A7"},
        "to": {"triple": ("Battery Pack BP-7", "max operating temperature"),
               "types": ["Requirement", "Constraint"]},
        "edge": "IMPLEMENTS", "weight": 0.92, "confidence": 0.9,
        "rationale": "A7 is the design that satisfies the pack thermal limit",
    },
    {
        "from": {"entity": "Type Certification"},
        "to": {"triple": ("Battery Pack BP-7", "max operating temperature"),
               "types": ["Requirement", "Constraint"]},
        "edge": "REQUIRES", "weight": 0.9, "confidence": 0.9,
        "rationale": "certification is granted against the thermal envelope",
    },
    {
        "from": {"decision": "submit Type Certification"},
        "to": {"entity": "Type Certification"},
        "edge": "AFFECTS", "weight": 0.85, "confidence": 0.85,
        "rationale": "the decision commits the certification submission",
    },
    {
        "from": {"triple": ("Battery Pack BP-7", "max operating temperature"),
                 "types": ["Requirement", "Constraint"]},
        "to": {"decision": "submit Type Certification"},
        "edge": "CONSTRAINS", "weight": 0.9, "confidence": 0.9,
        "rationale": "the submission is made against this envelope",
    },
    {
        "from": {"decision": "proceed with Fleet Deployment Phase 2"},
        "to": {"entity": "Type Certification"},
        "edge": "BASED_ON", "weight": 0.88, "confidence": 0.85,
        "rationale": "revenue service is gated on certification",
    },
    {
        "from": {"decision": "proceed with Fleet Deployment Phase 2"},
        "to": {"entity": "Thermal Architecture A7"},
        "edge": "BASED_ON", "weight": 0.8, "confidence": 0.8,
        "rationale": "the deployment assumes the passive cooling design holds",
    },
    {
        "from": {"decision": "selected Volta Cells Ltd"},
        "to": {"triple": ("Volta Cells Ltd", "capacity")},
        "edge": "BASED_ON", "weight": 0.9, "confidence": 0.9,
        "rationale": "monthly capacity was a selection criterion",
    },
    {
        "from": {"decision": "selected Volta Cells Ltd"},
        "to": {"triple": ("Volta Cells Ltd", "lead time")},
        "edge": "BASED_ON", "weight": 0.85, "confidence": 0.85,
        "rationale": "lead time was the deciding criterion",
    },
    {
        "from": {"entity": "Fleet Deployment Phase 2"},
        "to": {"triple": ("Volta Cells Ltd", "capacity")},
        "edge": "DEPENDS_ON", "weight": 0.85, "confidence": 0.85,
        "rationale": "build rate is bounded by cell supply",
    },
    {
        "from": {"risk": "thermal runaway"},
        "to": {"entity": "Thermal Architecture A7"},
        "edge": "DEPENDS_ON", "weight": 0.8, "confidence": 0.7,
        "rationale": "the risk is a property of the passive design",
    },
    {
        "from": {"risk": "certification"},
        "to": {"entity": "Type Certification"},
        "edge": "AFFECTS", "weight": 0.8, "confidence": 0.7,
        "rationale": "materialises as a certification outcome",
    },
]


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------
def _match(store: KnowledgeStore, spec: dict[str, Any]) -> KNode | None:
    if name := spec.get("entity"):
        return store.find_entity(name) or _by_label(store, name, NodeType.ENTITY)
    if triple := spec.get("triple"):
        types = {NodeType(t) for t in spec.get("types", [])} or None
        candidates = [
            n for n in store.siblings_of_triple(*triple)
            if (types is None or n.type in types) and n.status.is_live
        ]
        if candidates:
            return max(candidates, key=lambda n: (n.valid_from or n.created_at))
        return None
    for key, ntype in (("decision", NodeType.DECISION), ("risk", NodeType.RISK),
                       ("event", NodeType.EVENT), ("claim", NodeType.CLAIM)):
        if needle := spec.get(key):
            return _by_label(store, needle, ntype)
    return None


def _by_label(store: KnowledgeStore, needle: str, ntype: NodeType) -> KNode | None:
    low = needle.lower()
    matches = [
        n for n in store.of_type(ntype)
        if low in n.label.lower() or low in (n.body or "").lower()
    ]
    if not matches:
        return None
    return min(matches, key=lambda n: len(n.label))


def apply_curated_links(store: KnowledgeStore) -> tuple[int, list[str]]:
    """Add the cross-document structure. Returns (added, unresolved)."""
    added = 0
    unresolved: list[str] = []
    for spec in CURATED_LINKS:
        src = _match(store, spec["from"])
        dst = _match(store, spec["to"])
        if src is None or dst is None:
            which = "source" if src is None else "target"
            unresolved.append(f"{spec['edge']}: {which} not found "
                              f"({spec['from'] if src is None else spec['to']})")
            continue
        etype = EdgeType(spec["edge"])
        if store.find_edge(src.id, dst.id, etype):
            continue
        store.add_edge(KEdge(
            id=edge_id(), source=src.id, target=dst.id, type=etype,
            weight=spec.get("weight", 0.8), confidence=spec.get("confidence", 0.8),
            rationale=spec.get("rationale", "curated structural link"),
            attributes={"curated": True},
        ))
        added += 1
    return added, unresolved


# ---------------------------------------------------------------------------
# Seeding
# ---------------------------------------------------------------------------
def seed(
    store: KnowledgeStore | None = None,
    pipeline: Pipeline | None = None,
    *,
    copy_to_inbox: bool = True,
    project: bool = True,
) -> dict[str, Any]:
    """Build the baseline world. Idempotent: re-running changes nothing."""
    pipe = pipeline or Pipeline(store=store) if (pipeline or store) else Pipeline()
    st = pipe.store

    settings.ensure_dirs()
    results = []
    for doc in sorted(CORPUS.glob("*.md")):
        target = doc
        if copy_to_inbox:
            target = settings.incoming_dir / doc.name
            if not target.exists() or target.read_bytes() != doc.read_bytes():
                shutil.copy2(doc, target)
        r = pipe.ingest_file(target)
        results.append(r)
        log.info("seeded %s: +%d nodes +%d edges%s", doc.name, r.nodes_added,
                 r.edges_added, f" ({r.skipped_reason})" if r.skipped_reason else "")

    added, unresolved = apply_curated_links(st)
    for u in unresolved:
        log.warning("curated link unresolved -> %s", u)

    if project:
        from backend.vault.markdown_writer import project_all

        project_all(st)

    return {
        "documents": len(results),
        "nodes": len(st.nodes),
        "edges": len(st.edges),
        "curated_links_added": added,
        "curated_links_unresolved": unresolved,
        "stats": st.stats(),
    }


def stage_change(name: str, *, to_inbox: bool = True) -> Path:
    """Copy a change document into the inbox so the watcher picks it up."""
    src = CHANGES / name
    if not src.exists():
        raise FileNotFoundError(f"no change document {name} in {CHANGES}")
    if not to_inbox:
        return src
    settings.ensure_dirs()
    target = settings.incoming_dir / src.name
    shutil.copy2(src, target)
    return target


def available_changes() -> list[str]:
    return sorted(p.name for p in CHANGES.glob("*.md"))


def _purge(path: Path) -> None:
    """Remove a store whether it is a directory or a single file.

    Kuzu keeps the whole database in ONE file plus sidecars (.wal, .lock,
    .shadow); LanceDB uses a directory. Calling rmtree on the Kuzu file fails
    with "the directory name is invalid" -- and with ignore_errors=True that
    failure is silent, so a reset appears to succeed while leaving the entire
    graph in place.
    """
    if path.is_dir():
        shutil.rmtree(path, ignore_errors=True)
    elif path.exists():
        path.unlink(missing_ok=True)
    for sidecar in path.parent.glob(f"{path.name}.*"):
        if sidecar.is_dir():
            shutil.rmtree(sidecar, ignore_errors=True)
        else:
            sidecar.unlink(missing_ok=True)


def reset(confirm: bool = False) -> None:
    """Delete graph, vectors and vault. Explicit by design."""
    if not confirm:
        raise RuntimeError("reset() requires confirm=True")

    # Drop any live handle first: on Windows an open Kuzu database cannot be
    # deleted, and the in-process singleton would otherwise be rehydrated from
    # stale indices even after the files were removed.
    from backend.graph.store import set_store
    from backend.semantic.embeddings import set_index

    try:
        from backend.graph.store import _store as existing

        if existing is not None:
            existing.close()
    except Exception:
        pass
    set_store(None)
    set_index(None)

    for path in (settings.kuzu_dir, settings.lance_dir, settings.vault_dir):
        _purge(path)
    (settings.data_dir / "counters.json").unlink(missing_ok=True)
    for f in settings.incoming_dir.glob("*"):
        if f.is_file():
            f.unlink()

    leftovers = [p.name for p in (settings.kuzu_dir, settings.lance_dir) if p.exists()]
    if leftovers:
        log.warning("reset could not remove: %s", ", ".join(leftovers))
    else:
        log.info("demo state reset")
