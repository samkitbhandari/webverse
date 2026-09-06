"""Shared fixtures.

Every test gets its own Kuzu database, LanceDB directory and vault under tmp,
so the suite never touches the demo state in ``data/`` and tests cannot leak
into one another through the process-wide singletons.
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.core.ids import edge_id, node_id
from backend.core.models import (
    EdgeType,
    EpistemicStatus,
    KEdge,
    KNode,
    NodeType,
    Provenance,
)
from backend.core.units import parse_value
from backend.graph.store import KnowledgeStore, set_store
from backend.semantic.embeddings import VectorIndex, set_index


@pytest.fixture(autouse=True)
def isolated_paths(tmp_path, monkeypatch):
    """Point every filesystem-backed component at a per-test directory."""
    from backend.core.config import settings

    monkeypatch.setattr(settings, "data_dir", tmp_path / "data")
    monkeypatch.setattr(settings, "vault_dir", tmp_path / "vault")
    monkeypatch.setattr(settings, "incoming_dir", tmp_path / "incoming")
    settings.ensure_dirs()
    set_store(None)
    set_index(None)
    yield
    set_store(None)
    set_index(None)


@pytest.fixture
def store(tmp_path) -> KnowledgeStore:
    s = KnowledgeStore(kuzu_path=tmp_path / "kz")
    yield s
    s.close()


@pytest.fixture
def index(tmp_path) -> VectorIndex:
    return VectorIndex(path=tmp_path / "lance")


@pytest.fixture
def now() -> datetime:
    return datetime.now(timezone.utc)


@pytest.fixture
def make_node(store):
    def _make(ntype: NodeType, label: str, **kw) -> KNode:
        kw.setdefault("status", EpistemicStatus.SUPPORTED)
        kw.setdefault("confidence", 0.8)
        node = KNode(id=node_id(ntype), type=ntype, label=label, **kw)
        store.add_node(node)
        return node

    return _make


@pytest.fixture
def make_edge(store):
    def _make(src: KNode, dst: KNode, etype: EdgeType, **kw) -> KEdge:
        kw.setdefault("weight", 0.9)
        kw.setdefault("confidence", 0.9)
        edge = KEdge(id=edge_id(), source=src.id, target=dst.id, type=etype, **kw)
        store.add_edge(edge)
        return edge

    return _make


@pytest.fixture
def battery_world(store, make_node, make_edge, now):
    """The scenario the whole system is designed around, in miniature.

    A requirement, an observation that will violate its successor, an
    architecture implementing it, and a decision resting on the chain.
    """
    src = lambda name, auth, days=0: Provenance(  # noqa: E731
        source_path=f"{name}.pdf", source_label=name, authority=auth,
        observed_at=now - timedelta(days=days), extractor="test",
    )

    pack = make_node(NodeType.ENTITY, "Battery Pack BP-7")
    arch = make_node(NodeType.ENTITY, "Thermal Architecture A7")
    req = make_node(
        NodeType.REQUIREMENT, "BP-7 limit 70C",
        subject="Battery Pack BP-7", predicate="max operating temperature",
        operator="LTE", value=parse_value("70 degC"),
        valid_from=now - timedelta(days=400), confidence=0.9,
        provenance=[src("reg_2024", 0.85, 400)],
    )
    obs = make_node(
        NodeType.OBSERVATION, "measured 68C",
        subject="Battery Pack BP-7", predicate="max operating temperature",
        value=parse_value("68 degC"), valid_from=now - timedelta(days=30),
        confidence=0.88, provenance=[src("thermal_test", 0.8, 30)],
    )
    decision = make_node(
        NodeType.DECISION, "Deploy phase 2", valid_from=now - timedelta(days=60),
    )

    make_edge(arch, req, EdgeType.IMPLEMENTS)
    make_edge(req, decision, EdgeType.CONSTRAINS)
    make_edge(decision, arch, EdgeType.BASED_ON)
    make_edge(obs, pack, EdgeType.ABOUT, weight=0.6)
    make_edge(req, pack, EdgeType.ABOUT, weight=0.6)

    return {
        "store": store, "pack": pack, "arch": arch,
        "req": req, "obs": obs, "decision": decision,
    }
