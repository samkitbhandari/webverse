# NEXUS Ω

**When a regulation changes, which of our decisions just became invalid?**

That is the entire product. One question, answered with evidence.

---

## The problem

Priya runs certification for an electric vehicle programme. A revision to
UNECE R100 lands in her inbox: the battery operating limit drops from 70 °C to
60 °C.

She now has to work out what that breaks. The answer is not in the regulation —
it is spread across a thermal specification, a supplier agreement, a test
report and a certification plan, none of which mention each other. So it takes
a fortnight of meetings and a spreadsheet, and the honest output is *"we think
these are the affected items."*

Miss one and you find out at the certification audit.

This is not a search problem. Priya can already find the regulation. What she
cannot do is **traverse** — follow the new limit through the design that
implemented the old one, to the certification granted against it, to the
deployment decision that depends on that certification.

## What NEXUS does

Drop the revision into a folder. Within a second:

```
UNECE R100 rev.3  →  new limit 60 °C supersedes the old 70 °C limit
                  →  the 68 °C chamber measurement now violates it
                  →  2 decisions are no longer supported:

   severity 0.44   "proceed with Fleet Deployment Phase 2"
     because:  60 °C limit --SUPERSEDES--> 70 °C limit
               --REQUIRES--> Type Certification
               --BASED_ON--> Deploy Phase 2

   severity 0.27   "submit Type Certification against the current envelope"
```

Not a list of relevant documents. A list of **commitments that just broke**,
each with the chain of reasoning that broke it.

## Does it actually work?

```bash
python scripts/evaluate.py
```

Scored against human-written labels — written by reading the documents, *not*
by recording what the system outputs, so the gaps are real:

| Task | Score |
|---|---|
| Entity recognition | **100%** F1 |
| Assertion extraction | **91%** F1 |
| Temporal supersession | **100%** F1 |
| Contradiction detection | **100%** F1 |
| **Decision impact** | **100%** (2/2, with causal path) |

### Against a retrieval baseline

Same chunks, same embedder, generous top-10 — a third of the corpus in context,
and scored in retrieval's favour:

| | RAG baseline | NEXUS |
|---|---|---|
| Found the regulation text | yes | yes |
| Named the affected decisions | **1 of 2** | **2 of 2** |
| Explained *why* each is affected | no | yes |
| Ranked them by severity | no | yes |
| Traversed the dependency chain | no | yes |

Retrieval is genuinely good at finding the regulation. It cannot answer the
question that matters, because the chain crosses four documents and
"supersedes" and "depends on" do not exist in its representation. An LLM
reading those passages inherits the same ceiling — it can only reason over what
was retrieved.

The evaluation is under test (`tests/test_evaluation.py`), so a regression
fails the build rather than quietly lowering the number.

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
python scripts/forget.py         # inspect and remove knowledge (see below)
python -m pytest tests/ -q       # 114 tests
```

### Undoing an ingest

Deleting a file from `incoming_files/` **retires** what it supported rather
than erasing it: the claims survive at lower confidence and status
`UNVERIFIED`, because knowledge that was true because of a document does not
become false when the document is moved. That is the right default, and it is
not what you want after a bad ingest or a demo rehearsal.

To actually erase:

```bash
python scripts/forget.py --find "Supplier A capacity"
```

```bash
python scripts/forget.py --source "Supplier Notice"
```

Purging a source spares anything another document also asserts — corroborated
knowledge only loses that source's provenance. Run the script with no arguments
to see every source and how much of the graph it is solely responsible for. It
talks to the running API when there is one and opens the graph directly when
there is not, so you never have to stop the server to tidy up.

---

## What it actually does

The system thinks in beliefs, not files:

```
ENTITY   CLAIM   DECISION   REQUIREMENT   CONSTRAINT
ASSUMPTION   OBSERVATION   EVENT   RISK   ACTION   EVIDENCE
CONCEPT (derived by clustering, not asserted by any source)
```

Every belief carries **provenance** (which source, which sentence, how
authoritative), **confidence**, an **epistemic status** (supported, contested,
contradicted, superseded…) and a **validity interval**. Documents are evidence
providers, not the unit of knowledge.

### What the one answer requires

These are not six features. They are the six things that have to be true before
"which decisions just broke?" can be answered at all — each earns its place by
removing a specific way the answer would otherwise be wrong.

| Without it | The answer would be wrong because… |
|---|---|
| **Real-time ingestion** | …you would be reasoning over last month's documents. Debounced and stability-checked, so a half-written file is never parsed. |
| **Semantic compilation** | …"the limit is 60 °C" is prose. Until it is `(Battery Pack BP-7, max operating temperature, 60 degC)` nothing can compare it to anything. |
| **Temporal validity** | …the old 70 °C limit and the new 60 °C limit would both look current, and everything would contradict everything. |
| **Contradiction + credibility** | …you would get a flag, not a decision. Ranking says *which* source to believe and why. |
| **Impact propagation** | …you would get "these documents mention temperature" instead of "these two commitments broke". This is the traversal retrieval cannot do. |
| **Counterfactual simulation** | …you would know what broke but not what to do. Redesign the cooling or delay deployment — it prices both. |

### Emergent topics (embedding clustering)

Chunks are embedded on the way in, which is what makes retrieval work. The
clustering pass asks a further question: what does the embedding space say
about the knowledge *as a whole* — which beliefs are about the same thing even
when no document ever linked them?

Those groups become `Concept` nodes:

```bash
curl -X POST localhost:8000/api/concepts/rebuild -d '{"threshold":0.62}' -H 'Content-Type: application/json'
```

On the demo corpus this finds ~11 topics. The interesting ones are the
cross-document groups — a "max operating temperature" concept pulls together
the pack requirement, the chamber measurement and a corridor ambient reading
that live in three unrelated files.

Three properties make this safe rather than merely decorative:

- **Membership edges are nearly inert** (influence `0.08`). A concept touching
  twenty claims is a hub; if membership propagated like a dependency, every
  claim would reach every other in two hops and impact severity would become
  meaningless. There is a test that asserts exactly this.
- **Average linkage, not connected components.** Thresholding a similarity
  graph and taking components *chains*: A resembles B, B resembles C, so A and
  C merge even when unrelated. On a corpus with shared vocabulary that
  collapses into one blob.
- **Rebuilds replace, never accumulate.** Concepts are derived structure —
  `DELETE /api/concepts` removes them all and the asserted graph is untouched.

Clustering is on demand, not automatic: it is a global property of the whole
graph, so running it per-document would recompute everything each time and
churn concept ids under anyone reading them.

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
DEL  /api/nodes/{id}             erase a node, its edges, vector and vault note
DEL  /api/sources?path=…         erase what a source is solely responsible for
GET  /api/sources                every ingested source and its share of the graph
POST /api/concepts/rebuild       cluster embeddings into Concept nodes
GET  /api/concepts               emergent topics and their members
DEL  /api/concepts               drop all derived concepts
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
