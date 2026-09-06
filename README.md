# NEXUS Ω

**An autonomous semantic knowledge intelligence engine.**

> Traditional systems tell you what information exists.
> NEXUS Ω computes what changes when that information changes.

Drop a document into a folder. NEXUS parses it, compiles it into entities,
claims, requirements, decisions and events, resolves them against what it
already believes, notices what has changed or become contradictory, propagates
the consequences through the dependency graph, tells you which decisions now
need revalidating — and can simulate what would happen if you did something
different instead.

---

## Quick start

```bash
python -m venv .venv && .venv/Scripts/activate    # or: source .venv/bin/activate
pip install -r requirements.txt
cd frontend && npm install && cd ..
python scripts/dev.py --seed
```

Then open **http://127.0.0.1:3000**.

`--seed` builds the demo world and runs the full narrative once in the
terminal. Drop any `.md`, `.txt`, `.pdf` or `.docx` file into `incoming_files/`
and the graph updates live.

**No API key is required.** With no key configured the semantic compiler falls
back to a deterministic rule extractor and a hashing embedder, and the entire
pipeline — ingestion, contradiction detection, impact, counterfactuals — runs
offline. Set `OPENAI_API_KEY` or `ANTHROPIC_API_KEY` in `.env` to switch the
compiler to an LLM; nothing else changes. See [`.env.example`](.env.example).

```bash
python scripts/run_demo.py       # the whole loop, narrated in the terminal
python -m pytest tests/ -q       # 79 tests
```

---

## What it actually does

The system thinks in beliefs, not files:

```
ENTITY   CLAIM   DECISION   REQUIREMENT   CONSTRAINT
ASSUMPTION   OBSERVATION   EVENT   RISK   ACTION   EVIDENCE
```

Every belief carries **provenance** (which source, which sentence, how
authoritative), **confidence**, an **epistemic status** (supported, contested,
contradicted, superseded…) and a **validity interval**. Documents are evidence
providers, not the unit of knowledge.

### The six things it does well

| | |
|---|---|
| **Real-time ingestion** | `watchdog` on `incoming_files/`, debounced and stability-checked, normalised into one canonical event envelope |
| **Semantic compilation** | LLM structured extraction grounded in the existing graph, with a deterministic rule extractor as the floor |
| **Living temporal graph** | Kuzu for durable storage and Cypher; an indexed in-memory working set for reasoning; bitemporal validity |
| **Change + contradiction** | Semantic diff (not text diff), four contradiction kinds, resolved by ranked credibility |
| **Impact propagation** | Bounded max-product search over a direction-corrected dependency graph, with the explaining path |
| **Counterfactual simulation** | Clone the world, intervene, re-reason, diff the two futures on cost, schedule, risk and compliance |

---

## The demo narrative

The seeded world is a fictional autonomous-mobility programme: a shuttle fleet,
a traction battery, a thermal architecture, two cell suppliers, a certification
plan and the decisions resting on all of it.

Then a regulation lands in `incoming_files/`:

> UNECE R100 rev.3 reduced the battery operating limit from 70 °C to 60 °C.

What NEXUS does, unprompted:

```
new requirement  BP-7 ≤ 60 °C
      ↓ supersedes
old requirement  BP-7 ≤ 70 °C          validity closed, status SUPERSEDED
      ↓ constraint check
CONTRADICTION    observed 68 °C violates the new 60 °C limit
      ↓ credibility ranking
verdict          believe the regulation (authority 0.95, recency 1.00) over
                 the thermal test — margin +0.077
      ↓ impact propagation
19 nodes affected, 2 decisions need revalidation, max depth 4
      ↓ explanation
"BP-7 ≤ 60 °C --SUPERSEDES--> BP-7 ≤ 70 °C --REQUIRES--> Type Certification
 --BASED_ON--> Deploy Fleet Phase 2"
      ↓
knowledge health 86.7 → 76.4 (healthy → watch)
```

Then you ask *what if we redesign the cooling instead of delaying deployment?*
and it simulates both, on a detached clone, and reports the trade rather than a
winner:

```
A: redesign cooling      health 76.4 → 75.3   better: —      worse: cost, uncertainty
B: dual-source cells     health 76.4 → 75.6   better: capacity
C: delay deployment      health 76.4 → 68.0   worse: schedule, +1 decision at risk
RECOMMENDED: B
```

---

## Architecture

```
incoming_files/ ──watchdog──▶ canonical event ──▶ async worker (serialised)
                                                        │
   docling / pypdfium2 / python-docx ◀── parse ─────────┤
                                                        │
   LanceDB ◀── embed + retrieve candidates ─────────────┤
                                                        │
   LLM │ rules  ── semantic compilation ────────────────┤
                                                        │
   entity resolution (lexical + semantic + graph + type)┤
                                                        │
   reconciliation: new │ corroborating │ superseding │ contradictory
                                                        │
   Kuzu (durable) ◀── write-through ── in-memory working set
                                                        │
        ┌───────────────┬───────────────┬───────────────┤
   temporal        contradiction      impact       validation
   (bitemporal)    (credibility)   (max-product)     (value of info)
        └───────────────┴───────────────┴───────────────┤
                                                        │
   counterfactual simulator (clone → intervene → diff)  │
                                                        │
   FastAPI + WebSocket ──▶ Next.js UI    Markdown vault ┘
```

```
backend/
├── core/          config, knowledge model, unit normalisation, ids
├── ingestion/     watcher, canonical event bus, serialised worker
├── parsing/       docling with lightweight fallbacks, semantic chunking
├── semantic/      LLM providers, rule extractor, embeddings, entity resolution, semantic diff
├── graph/         Kuzu schema + store, in-memory working set, NetworkX algorithms
├── reasoning/     contradiction, temporal, impact, criticality, validation
├── counterfactual/simulator
├── vault/         Markdown projection
├── api/           REST routes + WebSocket
├── demo/          corpus, change documents, seed loader
└── pipeline.py    the orchestrator

frontend/          Next.js App Router, Tailwind, react-force-graph-2d
```

---

## Design decisions worth knowing

**Impact direction is a property of the edge type, not its orientation.**
`Decision --BASED_ON--> Claim` points decision-first, but a change in the claim
is what disturbs the decision — impact flows *backward* along it. The
`EDGE_DIRECTION` table in `core/models.py` is the most load-bearing thing in
the reasoning kernel; getting it wrong propagates confidently the wrong way.

**Supersession requires an explicit temporal signal.** When a new value
arrives for a property the graph already holds, three readings are possible:
the world changed, the sources disagree, or the source is restating itself.
Choosing by date alone is wrong — an undated report is not evidence that the
world moved. So supersession needs a stated date or a regulatory event;
everything else is treated as a contradiction, which is the conservative
reading because contradictions surface to a human while supersession silently
retires knowledge.

**Disjoint validity is succession, not conflict.** Two claims about the same
property over non-overlapping windows are a timeline, not a disagreement.
Missing this is the classic false positive.

**Values are normalised before they are compared.** "50,000 units/month" and
"50k units per month", "158 F" and "70 degC" must land on the same number and
unit or contradiction detection is theatre. Bare integers with no unit are
treated as identifiers, not measurements — otherwise "Phase 2" becomes a claim
that something equals 2.

**One node table, one edge table, discriminated by type.** Modelled literally,
eleven node labels and fifteen edge labels need up to 165 Kuzu rel-table
declarations and a label union on every traversal. The knowledge model is
polymorphic by design, so the type lives in an indexed property.

**Impact runs over retired nodes; belief queries do not.** A superseded
requirement is exactly *why* its dependents are disturbed, so filtering it out
would sever the path at the node that moved.

**The vault is a projection, never a source.** Notes are regenerated from the
graph and never read back, so the vault can be deleted and rebuilt at will.

**Simulation cannot reach the real graph.** A counterfactual clone is
constructed with `kuzu=None` — writes are structurally incapable of persisting.

---

## API

```
POST /api/ingest                 file path or inline text
POST /api/impact-analysis        propagate from seed nodes
POST /api/counterfactual         simulate one scenario
POST /api/counterfactual/compare rank several
POST /api/cypher                 read-only Cypher over the graph
GET  /api/graph                  force-graph payload
GET  /api/nodes, /api/nodes/{id}
GET  /api/entities|claims|decisions/{id}
GET  /api/timeline/{id}          value history of a property
GET  /api/as-of?when=…           belief state at a past instant
GET  /api/contradictions
GET  /api/knowledge-health
GET  /api/validation-queue       what to verify next, ranked by leverage
GET  /api/search                 semantic retrieval
WS   /ws                         live event stream
```

Interactive docs at http://127.0.0.1:8000/docs.

---

## Notes and limits

- **Python 3.10–3.12.** Kuzu has no 3.13/3.14 wheel yet and falls back to a
  source build that needs a C++ toolchain. If you only have 3.14,
  `uv venv --python 3.12 .venv` gets you a project-local interpreter.
- **Docling is optional** and deliberately not in `requirements.txt` — it pulls
  torch and roughly 3 GB of weights. Install `requirements-parsing.txt` for
  layout-aware PDF/DOCX extraction; without it PDFs go through pypdfium2 and
  DOCX through python-docx. `ParsedDocument.parser` records which ran.
- **The offline extractor is a pattern matcher, not a parser.** It reliably
  handles the statement shapes this domain is full of ("X must remain below N
  unit", "X capacity is N unit", "reduced from A to B"), emits lower
  confidence than an LLM, and tags everything `extractor="rules"` in
  provenance. Cross-document structure it cannot infer is declared explicitly
  in `backend/demo/seed.py` rather than smuggled into the corpus.
- **The hashing embedder captures lexical overlap, not meaning.** Good enough
  for candidate retrieval on a shared vocabulary; set an API key for real
  semantic similarity.
- Ingestion is serialised on purpose. Two documents reconciling against a graph
  that shifts underneath them produce duplicate entities and missed
  contradictions; ingest throughput is not the bottleneck here.
