"""The ingestion and reasoning pipeline.

One source event in, a reasoned change to the knowledge graph out:

    parse -> chunk -> embed -> retrieve candidates -> compile -> resolve
          -> reconcile -> persist -> supersede -> detect contradictions
          -> propagate impact -> project to Markdown -> broadcast

The interesting judgement lives in :func:`_reconcile_claim`. When a new value
arrives for a property the graph already holds, three readings are possible and
only one of them is right:

* the world changed          -> supersede the old belief
* the sources disagree       -> a contradiction, resolved on credibility
* the source restates itself -> corroboration, raising confidence

Choosing between them by date alone is wrong -- an undated report is not
evidence that the world moved. So supersession requires an *explicit* temporal
signal in the document; everything else is treated as disagreement, which is
the conservative reading because a contradiction is surfaced to a human while a
supersession silently retires knowledge.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from pydantic import BaseModel, Field

from backend.core.config import settings
from backend.core.ids import edge_id, fingerprint, node_id
from backend.core.models import (
    EdgeType,
    EpistemicStatus,
    Extraction,
    KEdge,
    KNode,
    NodeType,
    Provenance,
    SemanticEventType,
    SourceEventType,
    utcnow,
)
from backend.core.units import parse_value
from backend.graph.store import KnowledgeStore, get_store
from backend.ingestion.events import bus
from backend.parsing import docling_parser
from backend.reasoning import contradiction as contra
from backend.reasoning import temporal
from backend.reasoning.impact import ImpactReport, propagate
from backend.semantic import extractor
from backend.semantic.embeddings import VectorIndex, get_index
from backend.semantic.entity_resolution import resolve
from backend.semantic.semantic_diff import ChangeKind, SemanticChange, diff_claim

log = logging.getLogger("nexus.pipeline")

#: Authority we assign a source when the document says nothing about itself.
#: Regulations and standards outrank internal reports, which outrank notes.
_AUTHORITY_HINTS: list[tuple[str, float]] = [
    (r"regulation|directive|standard|unece|iso|iec|statute|law|compliance", 0.95),
    (r"contract|agreement|certification|audit", 0.88),
    (r"report|test|measurement|telemetry|analysis", 0.80),
    (r"spec|specification|requirement|design", 0.75),
    (r"minutes|memo|email|note|draft|slack", 0.55),
]


class IngestResult(BaseModel):
    source_path: str
    source_label: str
    parser: str = ""
    chunks_total: int = 0
    chunks_processed: int = 0
    nodes_added: int = 0
    nodes_updated: int = 0
    edges_added: int = 0
    entities_matched: int = 0
    entities_created: int = 0
    extractor: str = ""
    changes: list[SemanticChange] = Field(default_factory=list)
    contradictions: list[dict] = Field(default_factory=list)
    impact: ImpactReport | None = None
    skipped_reason: str = ""
    duration_ms: int = 0

    @property
    def material_changes(self) -> list[SemanticChange]:
        return [c for c in self.changes if c.is_material]


def _authority_for(path: Path, text: str) -> float:
    import re

    probe = f"{path.name} {text[:400]}".lower()
    for pattern, score in _AUTHORITY_HINTS:
        if re.search(pattern, probe):
            return score
    return 0.65


def _source_time(path: Path) -> datetime:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    except OSError:
        return utcnow()


def _parse_date(raw: str | None) -> datetime | None:
    if not raw:
        return None
    text = str(raw).strip()
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%d-%m-%Y", "%d/%m/%Y", "%Y-%m", "%Y"):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


class Pipeline:
    def __init__(self, store: KnowledgeStore | None = None, index: VectorIndex | None = None,
                 project_vault: bool = True):
        self.store = store or get_store()
        self.index = index if index is not None else get_index()
        self.project_vault = project_vault

    # -----------------------------------------------------------------
    # Entry points
    # -----------------------------------------------------------------
    def handle_source_event(self, event_type: SourceEventType, path: str) -> IngestResult | None:
        if event_type is SourceEventType.SOURCE_DELETED:
            return self.retire_source(path)
        return self.ingest_file(path)

    def retire_source(self, path: str) -> IngestResult:
        """A source disappeared: its evidence is gone, so its claims weaken.

        The claims are not deleted. Knowledge that was true because of a
        document does not become false because the document was moved -- but it
        does become unverified, and anything resting on it should be revisited.
        """
        result = IngestResult(source_path=path, source_label=Path(path).name)
        affected = self.store.nodes_from_source(path)
        for node in affected:
            if node.type is NodeType.EVIDENCE:
                self.store.update_node(
                    node.id, status=EpistemicStatus.INVALIDATED,
                    attributes={**node.attributes, "retired_at": utcnow().isoformat()},
                )
            elif node.status.is_live:
                self.store.update_node(
                    node.id,
                    status=EpistemicStatus.UNVERIFIED,
                    confidence=max(node.confidence * 0.7, 0.1),
                )
            result.nodes_updated += 1
        self.index.delete_source(path)
        bus.emit(
            SemanticEventType.GRAPH_CHANGED,
            {"reason": "source_removed", "path": path, "affected": result.nodes_updated},
            source_id=path,
        )
        log.info("retired source %s: %d node(s) weakened", Path(path).name, result.nodes_updated)
        return result

    def ingest_file(self, path: str | Path) -> IngestResult:
        started = utcnow()
        path = Path(path)
        result = IngestResult(source_path=str(path), source_label=path.name)

        bus.emit(SemanticEventType.PIPELINE_STAGE,
                 {"stage": "parsing", "source": path.name}, source_id=str(path))

        doc = docling_parser.parse(path)
        result.parser = doc.parser
        result.chunks_total = len(doc.chunks)
        if not doc.chunks:
            result.skipped_reason = f"no extractable content ({doc.parser})"
            log.info("skipping %s: %s", path.name, result.skipped_reason)
            return result

        evidence = self._ensure_evidence(path, doc)
        source_time = _source_time(path)
        authority = _authority_for(path, doc.markdown)

        # --- which chunks are actually new -----------------------------
        previous = set(evidence.attributes.get("chunk_fingerprints", []) or [])
        new_chunks = [c for c in doc.chunks if c.fingerprint not in previous]
        if previous and not new_chunks:
            result.skipped_reason = "content unchanged since last ingest"
            log.info("skipping %s: unchanged", path.name)
            return result
        result.chunks_processed = len(new_chunks)

        bus.emit(
            SemanticEventType.PIPELINE_STAGE,
            {"stage": "compiling", "source": path.name,
             "chunks": len(new_chunks), "of": len(doc.chunks)},
            source_id=str(path),
        )

        # --- index every chunk (even unchanged ones stay searchable) ----
        self.index.add([
            {
                "id": f"{path.name}:{c.index}",
                "kind": "chunk",
                "text": c.text,
                "source_path": str(path),
                "source_label": doc.title,
                "locator": c.locator,
            }
            for c in doc.chunks
        ])

        # --- compile ----------------------------------------------------
        known_entities = [n.label for n in self.store.of_type(NodeType.ENTITY)]
        known_predicates = sorted({
            n.predicate for n in self.store.nodes.values() if n.predicate
        })
        seeds: dict[str, float] = {}

        # The document's principal subject, refined as chunks are compiled. A
        # later section that says only "measured temperature reached 68 degC"
        # belongs to whatever the document has been talking about.
        subject_counts: dict[str, int] = {}

        for chunk in new_chunks:
            related = [
                r["text"][:200]
                for r in self.index.search(chunk.text, k=5, exclude_source=str(path))
            ]
            principal = (
                max(subject_counts, key=lambda s: subject_counts[s])
                if subject_counts else None
            )
            ex, used = extractor.compile_chunk(
                chunk.text,
                source_label=doc.title,
                known_entities=known_entities,
                known_predicates=known_predicates,
                related_claims=related,
                default_subject=principal,
            )
            for item in (*ex.claims, *ex.constraints):
                if item.subject:
                    subject_counts[item.subject] = subject_counts.get(item.subject, 0) + 1
            result.extractor = used
            prov = Provenance(
                source_id=evidence.id,
                source_path=str(path),
                source_label=doc.title,
                locator=chunk.locator or f"chunk {chunk.index}",
                excerpt=chunk.text[:400],
                authority=authority,
                observed_at=source_time,
                extractor=used,
            )
            self._apply(ex, prov, evidence, result, seeds, chunk.text)
            known_entities = [n.label for n in self.store.of_type(NodeType.ENTITY)]

        self.store.update_node(
            evidence.id,
            attributes={**evidence.attributes,
                        "chunk_fingerprints": [c.fingerprint for c in doc.chunks],
                        "parser": doc.parser},
        )

        # --- reason -----------------------------------------------------
        self._resolve_contradictions(result, seeds)
        if seeds:
            bus.emit(SemanticEventType.PIPELINE_STAGE,
                     {"stage": "impact", "source": path.name}, source_id=str(path))
            result.impact = propagate(self.store, seeds)
            if result.impact.total_affected:
                bus.emit(
                    SemanticEventType.IMPACT_COMPUTED,
                    {
                        "seeds": result.impact.seeds,
                        "seed_labels": result.impact.seed_labels,
                        "affected": result.impact.total_affected,
                        "decisions": [d.model_dump(mode="json")
                                      for d in result.impact.affected_decisions],
                        "top": [a.model_dump(mode="json") for a in result.impact.top(8)],
                    },
                    source_id=str(path),
                )
            for dec in result.impact.affected_decisions:
                bus.emit(SemanticEventType.DECISION_AFFECTED,
                         dec.model_dump(mode="json"), source_id=str(path))

        # --- project ----------------------------------------------------
        if self.project_vault:
            try:
                from backend.vault.markdown_writer import project_incremental

                touched = {c.old_node_id for c in result.changes if c.old_node_id}
                project_incremental(self.store, seeds | {t: 1.0 for t in touched if t})
            except Exception:
                log.exception("vault projection failed (knowledge graph is unaffected)")

        result.duration_ms = int((utcnow() - started).total_seconds() * 1000)
        bus.emit(
            SemanticEventType.GRAPH_CHANGED,
            {
                "source": path.name,
                "nodes_added": result.nodes_added,
                "edges_added": result.edges_added,
                "changes": len(result.material_changes),
                "contradictions": len(result.contradictions),
                "duration_ms": result.duration_ms,
                "extractor": result.extractor,
            },
            source_id=str(path),
        )
        log.info(
            "ingested %s in %dms: +%d nodes, +%d edges, %d change(s), %d contradiction(s)",
            path.name, result.duration_ms, result.nodes_added, result.edges_added,
            len(result.material_changes), len(result.contradictions),
        )
        return result

    # -----------------------------------------------------------------
    # Graph construction
    # -----------------------------------------------------------------
    def _ensure_evidence(self, path: Path, doc) -> KNode:
        fp = fingerprint("evidence", str(path))
        if existing := self.store.by_fp(fp):
            return existing
        node = KNode(
            id=node_id(NodeType.EVIDENCE),
            type=NodeType.EVIDENCE,
            label=doc.title or path.name,
            body=f"Source document: {path.name}",
            status=EpistemicStatus.SUPPORTED,
            confidence=0.9,
            fingerprint=fp,
            valid_from=_source_time(path),
            provenance=[Provenance(source_path=str(path), source_label=doc.title,
                                   authority=_authority_for(path, doc.markdown),
                                   observed_at=_source_time(path), extractor="ingest")],
            attributes={"path": str(path), "kind": "Document"},
        )
        self.store.add_node(node)
        self.index.add([{
            "id": f"node:{node.id}", "kind": "node", "node_id": node.id,
            "text": f"{node.label}. {node.body}", "source_path": str(path),
        }])
        return node

    def _link(self, source: str, target: str, etype: EdgeType, *, weight: float = 0.7,
              confidence: float = 0.7, rationale: str = "",
              provenance: Provenance | None = None) -> KEdge | None:
        if source == target or source not in self.store.nodes or target not in self.store.nodes:
            return None
        if existing := self.store.find_edge(source, target, etype):
            return existing
        return self.store.add_edge(KEdge(
            id=edge_id(), source=source, target=target, type=etype,
            weight=weight, confidence=confidence, rationale=rationale,
            provenance=provenance,
        ))

    def _apply(self, ex: Extraction, prov: Provenance, evidence: KNode,
               result: IngestResult, seeds: dict[str, float], chunk_text: str) -> None:
        local: dict[str, str] = {}          # extracted name -> node id

        # --- entities ---------------------------------------------------
        context_ids: list[str] = []
        for ent in ex.entities:
            res = resolve(self.store, ent.name, kind=ent.kind, context_text=chunk_text,
                          context_entity_ids=context_ids, index=self.index)
            if res.is_match:
                local[ent.name.lower()] = res.matched_id
                context_ids.append(res.matched_id)
                result.entities_matched += 1
                node = self.store.get(res.matched_id)
                if node and ent.description and not node.body:
                    self.store.update_node(res.matched_id, body=ent.description)
                continue

            node = KNode(
                id=node_id(NodeType.ENTITY), type=NodeType.ENTITY, label=ent.name,
                body=ent.description, status=EpistemicStatus.SUPPORTED,
                confidence=0.75, valid_from=prov.observed_at,
                fingerprint=fingerprint("entity", ent.name),
                provenance=[prov],
                attributes={"kind": ent.kind, "aliases": ent.aliases,
                            **({"resolution_candidates":
                                [c.node_id for c in res.candidates]} if res.needs_review else {})},
            )
            self.store.add_node(node)
            self.index.add([{
                "id": f"node:{node.id}", "kind": "node", "node_id": node.id,
                "text": f"{node.label}. {node.body}", "source_path": prov.source_path,
            }])
            local[ent.name.lower()] = node.id
            context_ids.append(node.id)
            result.nodes_added += 1
            result.entities_created += 1
            bus.emit(SemanticEventType.ENTITY_ADDED,
                     {"id": node.id, "label": node.label, "kind": ent.kind})

        def entity_for(name: str) -> str | None:
            if not name:
                return None
            if nid := local.get(name.lower()):
                return nid
            if hit := self.store.find_entity(name):
                return hit.id
            return None

        # --- claims / observations / assumptions ------------------------
        colocated: list[str] = []          # basis candidates for decisions below
        for claim in ex.claims:
            node = self._reconcile_claim(claim, prov, evidence, result, seeds)
            if node:
                local[claim.text.lower()[:60]] = node.id
                colocated.append(node.id)
                if eid := entity_for(claim.subject):
                    self._link(node.id, eid, EdgeType.ABOUT, weight=0.6,
                               confidence=0.85, rationale="claim is about this entity")
                    result.edges_added += 1

        # --- constraints / requirements ---------------------------------
        for con in ex.constraints:
            node = self._reconcile_constraint(con, prov, evidence, result, seeds)
            if node:
                local[con.text.lower()[:60]] = node.id
                colocated.append(node.id)
                if eid := entity_for(con.subject):
                    self._link(node.id, eid, EdgeType.ABOUT, weight=0.6, confidence=0.85)
                    result.edges_added += 1

        # --- decisions ---------------------------------------------------
        for dec in ex.decisions:
            fp = fingerprint("decision", dec.text)
            if self.store.by_fp(fp):
                continue
            node = KNode(
                id=node_id(NodeType.DECISION), type=NodeType.DECISION,
                label=dec.text[:110], body=dec.rationale,
                subject=dec.subject or None,
                status=EpistemicStatus.SUPPORTED, confidence=dec.confidence,
                valid_from=prov.observed_at, fingerprint=fp, provenance=[prov],
            )
            self.store.add_node(node)
            result.nodes_added += 1
            self._link(evidence.id, node.id, EdgeType.SUPPORTS, weight=0.8,
                       confidence=0.85, rationale="stated in source", provenance=prov)
            result.edges_added += 1
            if eid := entity_for(dec.subject):
                self._link(node.id, eid, EdgeType.AFFECTS, weight=0.7, confidence=0.75)
                result.edges_added += 1
            cited = False
            for basis in dec.based_on:
                if target := local.get(basis.lower()[:60]) or entity_for(basis):
                    self._link(node.id, target, EdgeType.BASED_ON, weight=0.85,
                               confidence=0.8, rationale=f"decision cites: {basis[:80]}")
                    result.edges_added += 1
                    cited = True
            if not cited:
                # No explicit citation. A decision stated in the same section as
                # a claim very often rests on it, so the link is recorded at
                # reduced weight and confidence, and says so. A weak, honest
                # lineage beats a decision with no lineage at all -- which the
                # health check would otherwise flag forever.
                for target in colocated[:4]:
                    self._link(node.id, target, EdgeType.BASED_ON, weight=0.45,
                               confidence=0.4,
                               rationale="inferred: stated in the same section")
                    result.edges_added += 1
            bus.emit(SemanticEventType.CLAIM_ADDED,
                     {"id": node.id, "type": "Decision", "label": node.label})

        # --- events -------------------------------------------------------
        for evt in ex.events:
            fp = fingerprint("event", evt.text)
            if self.store.by_fp(fp):
                continue
            node = KNode(
                id=node_id(NodeType.EVENT), type=NodeType.EVENT,
                label=evt.text[:110], subject=evt.subject or None,
                status=EpistemicStatus.SUPPORTED, confidence=evt.confidence,
                valid_from=_parse_date(evt.occurred_at) or prov.observed_at,
                fingerprint=fp, provenance=[prov],
                attributes={"occurred_at": evt.occurred_at},
            )
            self.store.add_node(node)
            result.nodes_added += 1
            seeds[node.id] = max(seeds.get(node.id, 0.0), 0.9)
            self._link(evidence.id, node.id, EdgeType.SUPPORTS, weight=0.85,
                       confidence=0.9, provenance=prov)
            result.edges_added += 1
            for target_name in evt.affects:
                if tid := entity_for(target_name):
                    self._link(node.id, tid, EdgeType.AFFECTS, weight=0.75,
                               confidence=0.7, rationale=evt.text[:120])
                    result.edges_added += 1

        # --- risks ---------------------------------------------------------
        for risk in ex.risks:
            fp = fingerprint("risk", risk.text)
            if self.store.by_fp(fp):
                continue
            node = KNode(
                id=node_id(NodeType.RISK), type=NodeType.RISK, label=risk.text[:110],
                subject=risk.subject or None, status=EpistemicStatus.UNVERIFIED,
                confidence=risk.confidence, valid_from=prov.observed_at,
                fingerprint=fp, provenance=[prov],
                attributes={"severity": risk.severity, "criticality": 0.7 + 0.3 * risk.severity},
            )
            self.store.add_node(node)
            result.nodes_added += 1
            if eid := entity_for(risk.subject):
                self._link(node.id, eid, EdgeType.AFFECTS, weight=0.7, confidence=0.6)
                result.edges_added += 1

        # --- explicit relationships -----------------------------------------
        for rel in ex.relationships:
            src = local.get(rel.source.lower()[:60]) or entity_for(rel.source)
            dst = local.get(rel.target.lower()[:60]) or entity_for(rel.target)
            if src and dst and self._link(src, dst, rel.type, weight=rel.weight,
                                          confidence=rel.confidence,
                                          rationale=rel.rationale[:200], provenance=prov):
                result.edges_added += 1
                bus.emit(SemanticEventType.RELATIONSHIP_ADDED,
                         {"source": src, "target": dst, "type": rel.type.value})

    # -----------------------------------------------------------------
    # Reconciliation
    # -----------------------------------------------------------------
    def _reconcile_claim(self, claim, prov: Provenance, evidence: KNode,
                         result: IngestResult, seeds: dict[str, float]) -> KNode | None:
        value = parse_value(claim.value, claim.unit)
        subject = claim.subject.strip()
        predicate = claim.predicate.strip().lower()
        if not subject or not predicate:
            return None

        ntype = {
            "Observation": NodeType.OBSERVATION,
            "Assumption": NodeType.ASSUMPTION,
        }.get(claim.kind, NodeType.CLAIM)

        fp = fingerprint("claim", subject, predicate, value.render(), ntype.value)
        if existing := self.store.by_fp(fp):
            # exact restatement: corroboration, not news
            self._corroborate(existing, prov, evidence, result)
            result.changes.append(diff_claim(existing, subject, predicate, value))
            return existing

        prior = self._current_belief(subject, predicate, ntype)
        change = diff_claim(prior, subject, predicate, value)
        result.changes.append(change)

        explicit_from = _parse_date(claim.valid_from)
        node = KNode(
            id=node_id(ntype), type=ntype,
            label=f"{subject} {predicate} = {value.render()}"[:110],
            body=claim.text,
            subject=subject, predicate=predicate, value=value,
            status=EpistemicStatus.SUPPORTED if claim.confidence >= 0.7
                   else EpistemicStatus.PROBABLE,
            confidence=claim.confidence,
            valid_from=explicit_from or prov.observed_at,
            valid_until=_parse_date(claim.valid_until),
            fingerprint=fp, provenance=[prov],
            attributes={"valid_from_explicit": explicit_from is not None},
        )
        self.store.add_node(node)
        result.nodes_added += 1
        self._link(evidence.id, node.id, EdgeType.SUPPORTS, weight=0.8,
                   confidence=0.85, rationale="asserted in source", provenance=prov)
        result.edges_added += 1
        self.index.add([{
            "id": f"node:{node.id}", "kind": "node", "node_id": node.id,
            "text": f"{node.label}. {node.body}", "source_path": prov.source_path,
        }])

        if change.is_material:
            seeds[node.id] = max(seeds.get(node.id, 0.0), max(change.magnitude, 0.35))
            bus.emit(
                change.event_type(),
                {"id": node.id, "label": node.label, **change.model_dump(mode="json")},
            )
            if change.kind is not ChangeKind.NEW:
                bus.emit(SemanticEventType.KNOWLEDGE_DRIFT, change.model_dump(mode="json"))
        return node

    def _reconcile_constraint(self, con, prov: Provenance, evidence: KNode,
                              result: IngestResult, seeds: dict[str, float]) -> KNode | None:
        value = parse_value(con.value, con.unit)
        subject = con.subject.strip()
        predicate = con.predicate.strip().lower()
        if not subject or not predicate:
            return None

        ntype = (NodeType.REQUIREMENT if con.kind == "Requirement" else NodeType.CONSTRAINT)
        fp = fingerprint("constraint", subject, predicate, con.operator,
                         value.render(), ntype.value)
        if existing := self.store.by_fp(fp):
            self._corroborate(existing, prov, evidence, result)
            return existing

        op = {"LT": "<", "LTE": "<=", "GT": ">", "GTE": ">=",
              "EQ": "=", "NEQ": "!="}.get(con.operator, con.operator)
        node = KNode(
            id=node_id(ntype), type=ntype,
            label=f"{subject} {predicate} {op} {value.render()}"[:110],
            body=con.text, subject=subject, predicate=predicate,
            value=value, operator=con.operator,
            status=EpistemicStatus.SUPPORTED, confidence=con.confidence,
            valid_from=prov.observed_at, fingerprint=fp, provenance=[prov],
        )
        self.store.add_node(node)
        result.nodes_added += 1
        self._link(evidence.id, node.id, EdgeType.SUPPORTS, weight=0.85,
                   confidence=0.9, provenance=prov)
        result.edges_added += 1
        seeds[node.id] = max(seeds.get(node.id, 0.0), 0.7)
        self.index.add([{
            "id": f"node:{node.id}", "kind": "node", "node_id": node.id,
            "text": f"{node.label}. {node.body}", "source_path": prov.source_path,
        }])
        return node

    def _corroborate(self, node: KNode, prov: Provenance, evidence: KNode,
                     result: IngestResult) -> None:
        """A second independent source saying the same thing raises confidence."""
        sources = {p.source_path for p in node.provenance}
        if prov.source_path in sources:
            return
        self.store.update_node(
            node.id,
            provenance=[*node.provenance, prov],
            confidence=min(node.confidence + 0.08, 0.99),
            status=EpistemicStatus.SUPPORTED if node.status.is_live else node.status,
        )
        self._link(evidence.id, node.id, EdgeType.SUPPORTS, weight=0.8,
                   confidence=0.85, rationale="corroborating source", provenance=prov)
        result.nodes_updated += 1
        result.edges_added += 1

    def _current_belief(self, subject: str, predicate: str, ntype: NodeType) -> KNode | None:
        """The live node currently asserting this property, if any."""
        candidates = [
            n for n in self.store.siblings_of_triple(subject, predicate)
            if n.status.is_live and n.type in (NodeType.CLAIM, NodeType.OBSERVATION,
                                               NodeType.ASSUMPTION)
        ]
        if not candidates:
            return None
        return max(candidates, key=lambda n: (n.valid_from or n.created_at))

    # -----------------------------------------------------------------
    # Post-ingest reasoning
    # -----------------------------------------------------------------
    def _resolve_contradictions(self, result: IngestResult, seeds: dict[str, float]) -> None:
        """Apply temporal succession first, then adjudicate what remains."""
        touched = [self.store.get(nid) for nid in list(seeds)]
        touched = [n for n in touched if n is not None]

        # 1. supersession -- only on an explicit temporal signal from the source
        for node in touched:
            if not node.attributes.get("valid_from_explicit") and node.type not in (
                NodeType.REQUIREMENT, NodeType.CONSTRAINT
            ):
                continue
            for old in temporal.find_predecessors(self.store, node):
                if old.id == node.id:
                    continue
                temporal.supersede(self.store, node, old,
                                   reason="a newer source states this property explicitly")
                result.nodes_updated += 1
                result.edges_added += 1
                bus.emit(SemanticEventType.CLAIM_SUPERSEDED,
                         {"new": node.id, "new_label": node.label,
                          "old": old.id, "old_label": old.label})

        # 2. contradictions among what is still standing
        for node in touched:
            current = self.store.get(node.id)
            if current is None or not current.status.is_live:
                continue
            for c in contra.detect_for_node(self.store, current):
                payload = c.model_dump(mode="json")
                if any(x["id"] == c.id for x in result.contradictions):
                    continue
                result.contradictions.append(payload)
                self._apply_verdict(c, result)
                bus.emit(
                    SemanticEventType.CONSTRAINT_VIOLATED
                    if c.kind is contra.ContradictionKind.CONSTRAINT
                    else SemanticEventType.CONTRADICTION_DETECTED,
                    payload,
                )

    def _apply_verdict(self, c, result: IngestResult) -> None:
        """Record the disagreement in the graph and update both sides' status."""
        left, right = self.store.get(c.left), self.store.get(c.right)
        if not (left and right):
            return

        self._link(right.id, left.id, EdgeType.CONTRADICTS, weight=0.9,
                   confidence=min(right.confidence, left.confidence),
                   rationale=c.description[:200])
        result.edges_added += 1

        for node in (left, right):
            new_status = contra.status_after(c, node.id)
            # A constraint is not "wrong" because reality violates it; the
            # observation is not wrong either. Both stand, and the violation is
            # the finding. Only value-vs-value conflicts change belief status.
            if c.kind is contra.ContradictionKind.CONSTRAINT:
                if node.type in (NodeType.CONSTRAINT, NodeType.REQUIREMENT):
                    continue
                new_status = EpistemicStatus.CONTESTED
            if node.status is not new_status:
                self.store.update_node(node.id, status=new_status)
                result.nodes_updated += 1


_pipeline: Pipeline | None = None


def get_pipeline() -> Pipeline:
    global _pipeline
    if _pipeline is None:
        _pipeline = Pipeline()
    return _pipeline


def set_pipeline(p: Pipeline | None) -> None:
    global _pipeline
    _pipeline = p
