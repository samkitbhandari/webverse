"""Kuzu schema for the temporal knowledge graph.

Design note -- why one node table and one rel table
---------------------------------------------------
The conceptual schema has eleven node labels and fifteen relationship labels.
Modelled literally in Kuzu that is 11 node tables and, because a rel table must
declare its FROM/TO pairs, up to 165 rel-table declarations; every traversal
would then need a label union and every new node type would be a migration.

The knowledge model is deliberately polymorphic -- a Claim can depend on a
Claim, a Decision, a Constraint or an Observation -- so we keep a single
``KNode`` table with a ``type`` discriminator and a single ``KEdge`` table with
a ``type`` discriminator. Traversal stays uniform, adding a node type is a
no-op, and the semantics live in indexed properties that Cypher can filter on:

    MATCH (d:KNode {type:'Decision'})-[r:KEdge {type:'BASED_ON'}]->(c:KNode)
    WHERE c.status = 'CONTRADICTED' RETURN d.id, c.id

Values are exploded into typed columns (``value_number``, ``value_unit``, ...)
rather than buried in JSON, because numeric comparison in Cypher is exactly
what constraint checking needs.
"""
from __future__ import annotations

NODE_TABLE = """
CREATE NODE TABLE IF NOT EXISTS KNode(
    id STRING,
    type STRING,
    label STRING,
    body STRING,
    subject STRING,
    predicate STRING,
    value_raw STRING,
    value_number DOUBLE,
    value_unit STRING,
    value_bool BOOLEAN,
    value_text STRING,
    operator STRING,
    status STRING,
    confidence DOUBLE,
    criticality DOUBLE,
    created_at STRING,
    updated_at STRING,
    valid_from STRING,
    valid_until STRING,
    valid_from_ts DOUBLE,
    valid_until_ts DOUBLE,
    fingerprint STRING,
    tags STRING[],
    provenance_json STRING,
    attributes_json STRING,
    PRIMARY KEY(id)
)
"""

EDGE_TABLE = """
CREATE REL TABLE IF NOT EXISTS KEdge(
    FROM KNode TO KNode,
    id STRING,
    type STRING,
    weight DOUBLE,
    confidence DOUBLE,
    created_at STRING,
    valid_from STRING,
    valid_until STRING,
    valid_from_ts DOUBLE,
    valid_until_ts DOUBLE,
    rationale STRING,
    provenance_json STRING,
    attributes_json STRING
)
"""

DDL = [NODE_TABLE, EDGE_TABLE]

#: Handy read-only queries the API exposes and the demo script uses.
NAMED_QUERIES: dict[str, str] = {
    "live_claims": (
        "MATCH (c:KNode) WHERE c.type = 'Claim' "
        "AND c.status <> 'SUPERSEDED' AND c.status <> 'INVALIDATED' "
        "RETURN c.id, c.label, c.status, c.confidence"
    ),
    "affected_decisions": (
        "MATCH (d:KNode)-[r:KEdge]->(c:KNode) "
        "WHERE d.type = 'Decision' AND r.type = 'BASED_ON' "
        "AND (c.status = 'CONTRADICTED' OR c.status = 'INVALIDATED' "
        "OR c.status = 'SUPERSEDED') "
        "RETURN DISTINCT d.id, d.label, c.id, c.status"
    ),
    "contradiction_pairs": (
        "MATCH (a:KNode)-[r:KEdge]->(b:KNode) WHERE r.type = 'CONTRADICTS' "
        "RETURN a.id, a.label, b.id, b.label, r.rationale"
    ),
    "superseded_chain": (
        "MATCH (new:KNode)-[r:KEdge]->(old:KNode) WHERE r.type = 'SUPERSEDES' "
        "RETURN new.id, new.label, old.id, old.label, r.created_at"
    ),
}
