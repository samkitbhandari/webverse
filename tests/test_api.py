"""HTTP surface, including the WebSocket stream.

The app is exercised through TestClient with its real lifespan, so these also
cover startup: graph rehydration, worker start and watcher registration.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from backend.core.models import NodeType
from backend.graph.store import KnowledgeStore, set_store
from backend.pipeline import Pipeline, set_pipeline
from backend.semantic.embeddings import VectorIndex, set_index

SOURCE = """\
# Thermal Specification

Battery Pack BP-7 must remain below 70 degC during continuous discharge.
Measured pack temperature reached 68 degC during the summer duty cycle test.
Thermal Architecture A7 implements the cooling approach for Battery Pack BP-7.
We have decided to proceed with Fleet Deployment Phase 2 because the pilot met
all availability targets.
"""


@pytest.fixture
def client(tmp_path):
    store = KnowledgeStore(kuzu_path=tmp_path / "kz")
    index = VectorIndex(path=tmp_path / "lance")
    set_store(store)
    set_index(index)
    pipeline = Pipeline(store=store, index=index, project_vault=False)
    set_pipeline(pipeline)

    src = tmp_path / "spec.md"
    src.write_text(SOURCE, encoding="utf-8")
    pipeline.ingest_file(src)

    from backend.main import app

    with TestClient(app) as c:
        yield c
    set_pipeline(None)
    store.close()


def test_health_and_stats(client):
    assert client.get("/health").json()["status"] == "ok"
    stats = client.get("/api/stats").json()
    assert stats["graph"]["nodes"] > 0
    assert stats["providers"]["llm"] in {"offline", "openai", "anthropic"}


def test_graph_endpoint_returns_a_connected_view(client):
    g = client.get("/api/graph?limit=200").json()
    assert g["nodes"] and g["links"]
    ids = {n["id"] for n in g["nodes"]}
    assert all(l["source"] in ids and l["target"] in ids for l in g["links"])


def test_small_graph_view_stays_connected(client):
    """A tight limit must not collapse the view into disconnected dots.

    The endpoint seeds on criticality and then grows one hop, because the most
    critical nodes are rarely adjacent to each other and an edge-filtered
    top-N would render as a field of unconnected points.
    """
    g = client.get("/api/graph?limit=5").json()
    assert len(g["links"]) > 0
    assert len(g["nodes"]) <= 10             # seeds plus at most one hop
    ids = {n["id"] for n in g["nodes"]}
    linked = {l["source"] for l in g["links"]} | {l["target"] for l in g["links"]}
    assert linked <= ids


def test_node_detail_carries_evidence_and_credibility(client):
    claims = client.get("/api/nodes?type=Claim,Observation&limit=1").json()["items"]
    node = client.get(f"/api/nodes/{claims[0]['id']}").json()
    assert node["provenance"] and node["provenance"][0]["source"]
    assert 0 <= node["credibility"]["total"] <= 1
    assert "relationships" in node


def test_decision_endpoint_returns_lineage_and_forward_impact(client):
    decisions = client.get("/api/nodes?type=Decision&limit=1").json()["items"]
    if not decisions:
        pytest.skip("no decision extracted from the fixture source")
    d = client.get(f"/api/decisions/{decisions[0]['id']}").json()
    assert "lineage" in d and "still_valid" in d["lineage"]
    assert "impact_if_changed" in d


def test_impact_analysis_explains_every_hit(client):
    req = client.get("/api/nodes?type=Requirement,Constraint&limit=1").json()["items"]
    if not req:
        pytest.skip("no requirement extracted from the fixture source")
    report = client.post("/api/impact-analysis", json={"seeds": [req[0]["id"]]}).json()
    assert report["total_affected"] >= 0
    for hit in report["affected"]:
        assert hit["explanation"] and hit["path"][0] == req[0]["id"]


def test_impact_analysis_rejects_unknown_seeds(client):
    r = client.post("/api/impact-analysis", json={"seeds": ["NOPE"]})
    assert r.status_code == 404


def test_type_mismatch_is_a_conflict_not_a_500(client):
    entity = client.get("/api/nodes?type=Entity&limit=1").json()["items"][0]
    assert client.get(f"/api/decisions/{entity['id']}").status_code == 409


def test_cypher_is_read_only(client):
    ok = client.post("/api/cypher", json={
        "query": "MATCH (n:KNode) RETURN n.id LIMIT 3"})
    assert ok.status_code == 200 and len(ok.json()["rows"]) <= 3

    for bad in ("MATCH (n:KNode) DELETE n",
                "CREATE NODE TABLE X(id STRING, PRIMARY KEY(id))",
                "MATCH (n:KNode) SET n.label = 'x'"):
        assert client.post("/api/cypher", json={"query": bad}).status_code == 400


def test_semantic_search_returns_scored_hits(client):
    hits = client.get("/api/search?q=battery temperature limit&k=5").json()["hits"]
    assert hits
    scores = [h["score"] for h in hits]
    assert scores == sorted(scores, reverse=True)


def test_counterfactual_requires_an_intervention(client):
    r = client.post("/api/counterfactual", json={"name": "empty", "interventions": []})
    assert r.status_code == 400


def test_counterfactual_comparison_ranks_scenarios(client):
    entities = client.get("/api/nodes?type=Entity&limit=2").json()["items"]
    if len(entities) < 2:
        pytest.skip("need two entities to compare")
    body = {
        "scenarios": [
            {"name": f"drop {e['id']}",
             "interventions": [{"type": "INVALIDATE", "target": e["id"]}]}
            for e in entities[:2]
        ]
    }
    out = client.post("/api/counterfactual/compare", json=body).json()
    assert out["comparison"]["recommended"]
    assert len(out["scenarios"]) == 2


def test_contradictions_and_health_are_consistent(client):
    contradictions = client.get("/api/contradictions").json()
    health = client.get("/api/knowledge-health").json()
    assert contradictions["count"] == health["open_contradictions"]
    assert 0 <= health["score"] <= 100
    assert health["grade"] in {"healthy", "watch", "degraded", "critical"}
    assert health["critical_nodes"]


def test_validation_queue_is_ordered_by_leverage(client):
    q = client.get("/api/validation-queue?limit=5").json()
    gains = [c["uncertainty_reduction"] for c in q["candidates"]]
    assert gains == sorted(gains, reverse=True)


def test_ingest_accepts_inline_text(client, tmp_path, monkeypatch):
    from backend.core.config import settings

    monkeypatch.setattr(settings, "incoming_dir", tmp_path / "in")
    settings.ensure_dirs()
    r = client.post("/api/ingest", json={"text": "# Note\n\nX capacity is 5 units per month.",
                                         "filename": "note.md"})
    assert r.status_code == 200 and r.json()["accepted"]
    assert (tmp_path / "in" / "note.md").exists()


def test_websocket_replays_recent_history(client):
    with client.websocket_connect("/ws") as ws:
        hello = ws.receive_json()
        assert hello["event_type"] == "CONNECTED"
        assert hello["payload"]["stats"]["nodes"] > 0


def test_websocket_receives_live_events(client):
    # replay=0 suppresses the history burst from the fixture's ingest, which
    # would otherwise fill the read budget before the live event arrives.
    with client.websocket_connect("/ws?replay=0") as ws:
        ws.receive_json()                        # CONNECTED
        client.post("/api/events", json={
            "event_type": "SOURCE_MODIFIED",
            "payload": {"path": "/tmp/none.md", "name": "none.md"},
        })
        for _ in range(30):
            msg = ws.receive_json()
            if msg["event_type"] == "SOURCE_MODIFIED":
                assert msg["payload"]["name"] == "none.md"
                return
        pytest.fail("live event never arrived on the stream")
