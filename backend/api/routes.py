"""REST surface.

Read endpoints answer questions about the current knowledge state; write
endpoints inject sources or events and let the pipeline do the reasoning. The
API deliberately exposes the *reasoning*, not just the data -- every impact,
contradiction and scenario response carries the path or the credibility
breakdown that produced it, because an answer a user cannot audit is not much
use in a system whose whole premise is explainability.
"""
from __future__ import annotations

import logging
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query, UploadFile, File
from pydantic import BaseModel, Field

from backend.core.config import settings
from backend.core.models import EdgeType, NodeType, SourceEventType
from backend.counterfactual import simulator
from backend.counterfactual.simulator import Intervention, InterventionType
from backend.graph import network_algorithms as algos
from backend.graph.store import get_store
from backend.ingestion.events import bus
from backend.ingestion.queue import worker
from backend.reasoning import temporal
from backend.reasoning.contradiction import credibility, detect_all
from backend.reasoning.criticality import knowledge_health, rank_critical
from backend.reasoning.impact import explain_why_affected, propagate
from backend.reasoning.validation import build_queue
from backend.semantic.embeddings import get_index

log = logging.getLogger("nexus.api")
router = APIRouter()


# ---------------------------------------------------------------------------
# Serialisation
# ---------------------------------------------------------------------------
def node_json(node, store=None) -> dict[str, Any]:
    d = {
        "id": node.id,
        "type": node.type.value,
        "label": node.label,
        "body": node.body,
        "subject": node.subject,
        "predicate": node.predicate,
        "value": node.value.render() if node.value else None,
        "value_number": node.value.number if node.value else None,
        "unit": node.value.unit if node.value else None,
        "operator": node.operator,
        "status": node.status.value,
        "confidence": round(node.confidence, 3),
        "criticality": round(node.criticality, 3),
        "created_at": node.created_at.isoformat(),
        "valid_from": node.valid_from.isoformat() if node.valid_from else None,
        "valid_until": node.valid_until.isoformat() if node.valid_until else None,
        "tags": node.tags,
        "attributes": node.attributes,
        "provenance": [
            {
                "source": p.source_label or p.source_path,
                "path": p.source_path,
                "locator": p.locator,
                "excerpt": p.excerpt,
                "authority": p.authority,
                "extractor": p.extractor,
                "observed_at": p.observed_at.isoformat() if p.observed_at else None,
            }
            for p in node.provenance
        ],
    }
    if store is not None:
        d["relationships"] = {
            "outgoing": [
                {"type": e.type.value, "target": e.target,
                 "target_label": (t.label if (t := store.get(e.target)) else e.target),
                 "weight": e.weight, "confidence": e.confidence,
                 "rationale": e.rationale}
                for e in store.out_edges(node.id)
            ],
            "incoming": [
                {"type": e.type.value, "source": e.source,
                 "source_label": (s.label if (s := store.get(e.source)) else e.source),
                 "weight": e.weight, "confidence": e.confidence,
                 "rationale": e.rationale}
                for e in store.in_edges(node.id)
            ],
        }
    return d


def _require(node_id: str, *types: NodeType):
    store = get_store()
    node = store.get(node_id)
    if node is None:
        raise HTTPException(404, f"no node {node_id}")
    if types and node.type not in types:
        raise HTTPException(
            409,
            f"{node_id} is a {node.type.value}, not a "
            f"{' or '.join(t.value for t in types)}",
        )
    return store, node


# ---------------------------------------------------------------------------
# Graph reads
# ---------------------------------------------------------------------------
@router.get("/graph")
def get_graph(
    limit: int = Query(400, ge=1, le=5000),
    types: str | None = Query(None, description="comma-separated node types"),
    include_retired: bool = True,
):
    """The whole graph, shaped for the force-directed view."""
    store = get_store()
    wanted = None
    if types:
        wanted = {t.strip().lower() for t in types.split(",") if t.strip()}

    eligible = []
    for n in store.nodes.values():
        if wanted and n.type.value.lower() not in wanted:
            continue
        if not include_retired and not n.status.is_live:
            continue
        eligible.append(n)
    eligible.sort(key=lambda n: (-n.criticality, n.id))

    # Taking the top-N by criticality and then keeping only edges whose BOTH
    # endpoints survived produces a field of disconnected dots -- the most
    # critical nodes are rarely adjacent to each other. So the selection is
    # grown one hop from the seeds, which keeps the view connected.
    eligible_ids = {n.id for n in eligible}
    selected = {n.id for n in eligible[:limit]}
    room = max(limit * 2 - len(selected), 0)
    for nid in list(selected):
        if room <= 0:
            break
        for e in store.incident(nid):
            other = e.target if e.source == nid else e.source
            if other in selected or other not in eligible_ids:
                continue
            selected.add(other)
            room -= 1
            if room <= 0:
                break

    nodes = [n for n in eligible if n.id in selected]
    edges = [e for e in store.edges.values()
             if e.source in selected and e.target in selected]
    payload = algos.to_cytoscape(nodes, edges)
    payload["stats"] = store.stats()
    payload["truncated"] = len(eligible) > len(nodes)
    return payload


@router.get("/graph/around/{node_id}")
def graph_around(node_id: str, depth: int = Query(2, ge=1, le=4),
                 limit: int = Query(200, ge=1, le=2000)):
    store, _ = _require(node_id)
    nodes, edges = algos.subgraph_around(store, node_id, depth=depth, limit=limit)
    payload = algos.to_cytoscape(nodes, edges)
    payload["focus"] = node_id
    return payload


@router.get("/nodes")
def list_nodes(
    type: str | None = None,
    status: str | None = None,
    q: str | None = None,
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
):
    store = get_store()
    items = list(store.nodes.values())
    if type:
        wanted = {t.strip().lower() for t in type.split(",")}
        items = [n for n in items if n.type.value.lower() in wanted]
    if status:
        wanted = {s.strip().upper() for s in status.split(",")}
        items = [n for n in items if n.status.value in wanted]
    if q:
        needle = q.lower()
        items = [
            n for n in items
            if needle in n.label.lower() or needle in (n.body or "").lower()
            or needle in (n.subject or "").lower()
        ]
    items.sort(key=lambda n: (-n.criticality, n.id))
    total = len(items)
    return {
        "total": total,
        "offset": offset,
        "items": [node_json(n) for n in items[offset: offset + limit]],
    }


@router.get("/nodes/{node_id}")
def get_node(node_id: str):
    store, node = _require(node_id)
    data = node_json(node, store)
    data["credibility"] = credibility(node, store).model_dump()
    return data


@router.get("/entities/{node_id}")
def get_entity(node_id: str):
    store, node = _require(node_id, NodeType.ENTITY)
    data = node_json(node, store)
    data["claims"] = [
        node_json(store.get(e.source))
        for e in store.in_edges(node_id)
        if e.type is EdgeType.ABOUT and store.get(e.source)
    ]
    return data


@router.get("/claims/{node_id}")
def get_claim(node_id: str):
    store, node = _require(node_id, NodeType.CLAIM, NodeType.OBSERVATION,
                           NodeType.ASSUMPTION)
    data = node_json(node, store)
    data["credibility"] = credibility(node, store).model_dump()
    tl = temporal.node_timeline(store, node_id)
    data["timeline"] = tl.model_dump(mode="json") if tl else None
    return data


@router.get("/decisions/{node_id}")
def get_decision(node_id: str):
    store, node = _require(node_id, NodeType.DECISION)
    data = node_json(node, store)
    data["lineage"] = temporal.beliefs_behind_decision(store, node_id)
    data["impact_if_changed"] = propagate(store, [node_id], max_depth=3).model_dump(
        mode="json"
    )
    return data


@router.get("/timeline/{node_id}")
def get_timeline(node_id: str):
    store, _ = _require(node_id)
    tl = temporal.node_timeline(store, node_id)
    if tl is None:
        raise HTTPException(404, f"no timeline for {node_id}")
    return tl.model_dump(mode="json")


@router.get("/as-of")
def get_as_of(when: datetime = Query(..., description="ISO-8601 instant")):
    """Reconstruct the belief state at a past moment."""
    store = get_store()
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    nodes = temporal.as_of(store, when)
    return {
        "as_of": when.isoformat(),
        "count": len(nodes),
        "nodes": [node_json(n) for n in sorted(nodes, key=lambda n: n.id)[:400]],
    }


# ---------------------------------------------------------------------------
# Reasoning
# ---------------------------------------------------------------------------
class ImpactRequest(BaseModel):
    seeds: list[str] = Field(..., description="node ids where the change starts")
    magnitudes: dict[str, float] | None = None
    max_depth: int = 6
    min_score: float | None = None


@router.post("/impact-analysis")
def impact_analysis(req: ImpactRequest):
    store = get_store()
    missing = [s for s in req.seeds if s not in store.nodes]
    if missing:
        raise HTTPException(404, f"unknown node(s): {', '.join(missing)}")
    seeds: Any = req.magnitudes or req.seeds
    report = propagate(store, seeds, max_depth=req.max_depth, min_score=req.min_score)
    return report.model_dump(mode="json")


@router.get("/impact/{node_id}/why/{target_id}")
def why_affected(node_id: str, target_id: str, max_depth: int = 6):
    store, _ = _require(node_id)
    report = propagate(store, [node_id], max_depth=max_depth)
    return {"explanation": explain_why_affected(store, report, target_id)}


@router.get("/contradictions")
def get_contradictions(kind: str | None = None, limit: int = Query(100, ge=1, le=500)):
    store = get_store()
    items = detect_all(store)
    if kind:
        wanted = {k.strip().upper() for k in kind.split(",")}
        items = [c for c in items if c.kind.value in wanted]
    return {
        "count": len(items),
        "items": [c.model_dump(mode="json") for c in items[:limit]],
    }


@router.get("/knowledge-health")
def get_health():
    store = get_store()
    health = knowledge_health(store).model_dump(mode="json")
    health["critical_nodes"] = [c.model_dump() for c in rank_critical(store, 8)]
    health["centrality"] = algos.centrality_report(store, top_k=8)
    return health


@router.get("/validation-queue")
def get_validation_queue(limit: int = Query(10, ge=1, le=50)):
    return build_queue(get_store(), limit=limit).model_dump(mode="json")


@router.get("/search")
def search(q: str = Query(..., min_length=2), k: int = Query(10, ge=1, le=50)):
    """Semantic retrieval over indexed chunks and node texts."""
    store = get_store()
    hits = get_index().search(q, k=k)
    for h in hits:
        if nid := h.get("node_id"):
            if node := store.get(nid):
                h["node"] = node_json(node)
    return {"query": q, "hits": hits}


class CypherRequest(BaseModel):
    query: str
    params: dict[str, Any] | None = None


@router.post("/cypher")
def run_cypher(req: CypherRequest):
    """Read-only Cypher against the persistent graph."""
    lowered = req.query.strip().lower()
    forbidden = ("create ", "delete ", "set ", "merge ", "drop ", "detach ", "copy ")
    if any(tok in f" {lowered} " for tok in forbidden):
        raise HTTPException(400, "only read queries are permitted on this endpoint")
    try:
        return {"rows": get_store().query(req.query, req.params)}
    except Exception as exc:
        raise HTTPException(400, f"query failed: {exc}") from exc


# ---------------------------------------------------------------------------
# Counterfactual
# ---------------------------------------------------------------------------
class ScenarioRequest(BaseModel):
    name: str = "scenario"
    description: str = ""
    interventions: list[Intervention]
    max_depth: int = 5


class CompareRequest(BaseModel):
    scenarios: list[ScenarioRequest]


@router.post("/counterfactual")
def counterfactual(req: ScenarioRequest):
    store = get_store()
    if not req.interventions:
        raise HTTPException(400, "a scenario needs at least one intervention")
    result = simulator.simulate(
        store, req.interventions, name=req.name, description=req.description,
        max_depth=req.max_depth,
    )
    return result.model_dump(mode="json")


@router.post("/counterfactual/compare")
def counterfactual_compare(req: CompareRequest):
    store = get_store()
    if len(req.scenarios) < 2:
        raise HTTPException(400, "comparison needs at least two scenarios")
    results = [
        simulator.simulate(store, s.interventions, name=s.name,
                           description=s.description, max_depth=s.max_depth)
        for s in req.scenarios
    ]
    return {
        "comparison": simulator.compare(results),
        "scenarios": [r.model_dump(mode="json") for r in results],
    }


# ---------------------------------------------------------------------------
# Ingestion
# ---------------------------------------------------------------------------
class IngestRequest(BaseModel):
    path: str | None = None
    text: str | None = None
    filename: str | None = None


@router.post("/ingest")
def ingest(req: IngestRequest):
    """Ingest a file already on disk, or inline text saved into the inbox.

    Both routes end the same way: a file lands in ``incoming_files`` and the
    watcher event drives the pipeline. There is one ingestion path, so the API
    and the filesystem can never diverge in behaviour.
    """
    settings.ensure_dirs()
    if req.text is not None:
        name = req.filename or f"api-{datetime.now():%Y%m%d-%H%M%S}.md"
        target = settings.incoming_dir / Path(name).name
        target.write_text(req.text, encoding="utf-8")
    elif req.path:
        src = Path(req.path)
        if not src.exists():
            raise HTTPException(404, f"no such file: {src}")
        target = settings.incoming_dir / src.name
        if src.resolve() != target.resolve():
            shutil.copy2(src, target)
    else:
        raise HTTPException(400, "provide either 'path' or 'text'")

    bus.emit(SourceEventType.SOURCE_CREATED,
             {"path": str(target), "name": target.name, "via": "api"},
             source_id=str(target))
    return {"accepted": True, "path": str(target),
            "note": "queued; watch the /ws stream for pipeline events"}


@router.post("/ingest/upload")
async def ingest_upload(file: UploadFile = File(...)):
    settings.ensure_dirs()
    target = settings.incoming_dir / Path(file.filename or "upload.bin").name
    target.write_bytes(await file.read())
    bus.emit(SourceEventType.SOURCE_CREATED,
             {"path": str(target), "name": target.name, "via": "upload"},
             source_id=str(target))
    return {"accepted": True, "path": str(target)}


class ClusterRequest(BaseModel):
    threshold: float = Field(
        0.62, ge=0.05, le=1.0,
        description="cosine distance ceiling; lower = tighter, more clusters",
    )
    min_size: int = Field(2, ge=2, le=50)
    max_size: int = Field(25, ge=2, le=500)


@router.post("/concepts/rebuild")
def rebuild_concepts(req: ClusterRequest | None = None):
    """Cluster the graph's embeddings and write the groups back as Concepts.

    Deliberately on demand rather than after every ingest: clustering is a
    global property of the whole graph, so it would be recomputed from scratch
    on every document, and concept ids would churn under anyone reading them.
    """
    from backend.semantic.clustering import build_concepts

    opts = req or ClusterRequest()
    return build_concepts(
        get_store(),
        threshold=opts.threshold,
        min_size=opts.min_size,
        max_size=opts.max_size,
    )


@router.get("/concepts")
def list_concepts():
    """Current emergent topics and their members."""
    from backend.semantic.clustering import concept_summary

    concepts = concept_summary(get_store())
    return {"count": len(concepts), "concepts": concepts}


@router.delete("/concepts")
def drop_concepts():
    """Remove all derived concepts, leaving the asserted graph untouched."""
    from backend.semantic.clustering import clear_concepts

    return {"removed": clear_concepts(get_store())}


@router.delete("/nodes/{node_id}")
def forget_node(node_id: str):
    """Erase one node, its relationships, its vector and its vault note.

    Destructive and irreversible, which is why it is a separate verb from
    removing a source file: deleting a file *retires* the knowledge it
    supported, this *forgets* it.
    """
    from backend.pipeline import get_pipeline

    result = get_pipeline().forget_node(node_id)
    if not result["removed"]:
        raise HTTPException(404, result["reason"])
    return result


@router.delete("/sources")
def purge_source(path: str = Query(..., description="source path to erase")):
    """Erase everything a source is solely responsible for.

    Knowledge that another document also asserts survives; it just loses this
    source's provenance. Use it to undo a bad ingest without resetting.
    """
    from backend.pipeline import get_pipeline

    return get_pipeline().purge_source(path)


@router.get("/sources")
def list_sources():
    """Every source the graph has ingested, and how much it is responsible for."""
    store = get_store()
    counts: dict[str, dict[str, Any]] = {}
    for node in store.nodes.values():
        for p in node.provenance:
            if not p.source_path:
                continue
            row = counts.setdefault(
                p.source_path,
                {"path": p.source_path, "label": p.source_label, "nodes": 0,
                 "sole_evidence": 0},
            )
            row["nodes"] += 1
            if len({q.source_path for q in node.provenance}) == 1:
                row["sole_evidence"] += 1
    return {"sources": sorted(counts.values(), key=lambda r: -r["nodes"])}


class EventRequest(BaseModel):
    event_type: str
    payload: dict[str, Any] = Field(default_factory=dict)
    source_id: str | None = None


@router.post("/events")
def post_event(req: EventRequest):
    """Inject a canonical event (used by external producers and the demo)."""
    try:
        etype: Any = SourceEventType(req.event_type)
    except ValueError:
        from backend.core.models import SemanticEventType

        try:
            etype = SemanticEventType(req.event_type)
        except ValueError as exc:
            raise HTTPException(400, f"unknown event type {req.event_type}") from exc
    ev = bus.emit(etype, req.payload, source_id=req.source_id)
    return ev.ws()


@router.get("/events/recent")
def recent_events(limit: int = Query(50, ge=1, le=300), types: str | None = None):
    wanted = [t.strip() for t in types.split(",")] if types else None
    return {"events": [e.ws() for e in bus.recent(limit, wanted)]}


# ---------------------------------------------------------------------------
# System
# ---------------------------------------------------------------------------
@router.get("/stats")
def stats():
    from backend.semantic.embeddings import get_embedder
    from backend.semantic.llm import get_llm

    store = get_store()
    return {
        "graph": store.stats(),
        "vectors": get_index().count(),
        "worker": worker.stats(),
        "subscribers": bus.subscriber_count,
        "providers": {"llm": get_llm().name, "embedder": get_embedder().name},
        "paths": {
            "incoming": str(settings.incoming_dir),
            "vault": str(settings.vault_dir),
            "data": str(settings.data_dir),
        },
    }


@router.post("/vault/project")
def project_vault():
    from backend.vault.markdown_writer import project_all

    count = project_all(get_store())
    return {"written": count, "vault": str(settings.vault_dir)}
