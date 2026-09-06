"""End-to-end demo of NEXUS Omega, narrated.

Runs the whole loop without the API or the UI, so the reasoning can be read in
a terminal:

    observe -> understand -> update beliefs -> check consistency
            -> propagate impact -> simulate alternatives -> explain

Usage:
    python scripts/run_demo.py            # reset, seed, run the full narrative
    python scripts/run_demo.py --keep     # keep existing state, apply changes only
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.counterfactual import simulator
from backend.counterfactual.simulator import Intervention, InterventionType
from backend.demo import seed as demo
from backend.graph.store import get_store
from backend.pipeline import Pipeline
from backend.reasoning import temporal
from backend.reasoning.contradiction import detect_all
from backend.reasoning.criticality import knowledge_health, rank_critical
from backend.reasoning.impact import explain_why_affected, propagate
from backend.reasoning.validation import build_queue

W = 78


def rule(title: str = "") -> None:
    if title:
        print(f"\n{'=' * W}\n{title}\n{'=' * W}")
    else:
        print("-" * W)


def health_line(store, label: str) -> None:
    h = knowledge_health(store)
    print(f"  {label:<22} {h.score:>5.1f}/100  {h.grade:<9} "
          f"contradictions={h.open_contradictions}  "
          f"decisions_at_risk={h.decisions_at_risk}  "
          f"uncertainty={h.total_uncertainty:.2f}")


def find_by_triple(store, subject: str, predicate: str, live_only: bool = True):
    items = store.siblings_of_triple(subject, predicate)
    if live_only:
        items = [n for n in items if n.status.is_live]
    return max(items, key=lambda n: (n.valid_from or n.created_at)) if items else None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep", action="store_true", help="do not reset existing state")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)-7s %(name)-20s %(message)s",
    )

    if not args.keep:
        demo.reset(confirm=True)


    from backend.semantic.embeddings import get_embedder
    from backend.semantic.llm import get_llm

    store = get_store()
    pipe = Pipeline(store=store)

    rule("NEXUS OMEGA - AUTONOMOUS SEMANTIC KNOWLEDGE INTELLIGENCE ENGINE")
    print(f"  semantic compiler : {get_llm().name}")
    print(f"  embedder          : {get_embedder().name}")

    # ---------------------------------------------------------------- 1
    rule("1. OBSERVE - build the baseline world from the source corpus")
    info = demo.seed(pipeline=pipe, project=True)
    print(f"  {info['documents']} documents -> {info['nodes']} nodes, "
          f"{info['edges']} relationships")
    print(f"  curated cross-document links added: {info['curated_links_added']}")
    for u in info["curated_links_unresolved"]:
        print(f"    ! unresolved: {u}")
    types = {k.replace("type_", ""): v for k, v in info["stats"].items()
             if k.startswith("type_")}
    print("  composition: " + ", ".join(f"{k}={v}" for k, v in sorted(types.items())))
    health_line(store, "baseline health")

    print("\n  most critical knowledge:")
    for c in rank_critical(store, 5):
        print(f"    {c.criticality:.3f}  {c.type:<12} {c.label[:46]}")

    # ---------------------------------------------------------------- 2
    rule("2. UNDERSTAND - a regulation lands in incoming_files/")
    path = demo.stage_change("06_regulation_update.md")
    print(f"  new source: {path.name}")
    result = pipe.ingest_file(path)
    print(f"  parsed by {result.parser}, compiled by {result.extractor} "
          f"({result.chunks_processed} chunk(s)) in {result.duration_ms} ms")
    print(f"  +{result.nodes_added} nodes  +{result.edges_added} relationships")

    if result.material_changes:
        print("\n  SEMANTIC CHANGE (not a text diff):")
        for c in result.material_changes:
            print(f"    [{c.kind.value}] {c.description}")
            if c.delta is not None:
                print(f"        magnitude {c.magnitude:.2f}, direction {c.direction}")

    # ---------------------------------------------------------------- 3
    rule("3. CHECK CONSISTENCY - what became contradictory")
    if result.contradictions:
        for c in result.contradictions:
            print(f"  [{c['kind']}] severity {c['severity']:.2f}")
            print(f"    {c['description']}")
            print(f"    verdict: {c['verdict']}")
    else:
        print("  none raised during this ingest")

    all_c = detect_all(store)
    print(f"\n  open contradictions across the whole graph: {len(all_c)}")

    print("\n  TEMPORAL SUCCESSION for Battery Pack BP-7 / max operating temperature:")
    for pt in temporal.timeline(store, "Battery Pack BP-7", "max operating temperature").points:
        window = (f"{pt.valid_from.date() if pt.valid_from else '-'} .. "
                  f"{pt.valid_until.date() if pt.valid_until else 'present'}")
        print(f"    {pt.node_id:<6} {str(pt.value):<12} {pt.status:<12} {window}")

    # ---------------------------------------------------------------- 4
    rule("4. PROPAGATE IMPACT - what else moved")
    if result.impact and result.impact.total_affected:
        rep = result.impact
        print(f"  {rep.total_affected} node(s) affected, "
              f"{len(rep.affected_decisions)} decision(s), "
              f"max depth {rep.max_depth_reached}")
        print()
        for a in rep.top(10):
            print(f"    sev {a.severity:.3f} | d{a.depth} | {a.type:<12} {a.label[:44]}")
        if rep.affected_decisions:
            target = rep.affected_decisions[0]
            print("\n  " + "-" * (W - 2))
            print(explain_why_affected(store, rep, target.id))
    else:
        print("  no downstream propagation recorded")

    # ---------------------------------------------------------------- 5
    rule("5. DECISION LINEAGE - is the decision still valid?")
    from backend.core.models import NodeType

    for d in sorted(store.of_type(NodeType.DECISION), key=lambda n: n.id):
        lin = temporal.beliefs_behind_decision(store, d.id)
        flag = "AT RISK" if not lin["still_valid"] else "ok"
        print(f"\n  [{flag}] {d.label[:60]}")
        print(f"     {lin['summary']}")
        for b in lin["basis"]:
            mark = "!" if b["changed"] else " "
            print(f"     {mark} {b['id']:<6} {b['relationship']:<12} "
                  f"{b['label'][:38]:<40} now={b['status_now']}")

    # ---------------------------------------------------------------- 6
    rule("6. SIMULATE ALTERNATIVES - counterfactual scenarios")
    obs = find_by_triple(store, "Battery Pack BP-7", "max operating temperature")
    arch = store.find_entity("Thermal Architecture A7")
    phase2 = store.find_entity("Fleet Deployment Phase 2")
    volta_cap = find_by_triple(store, "Volta Cells Ltd", "capacity")
    kirin = store.find_entity("Kirin Energy Systems")

    scenarios: list[tuple[str, str, list[Intervention]]] = []

    if obs is not None:
        scenarios.append((
            "A: redesign cooling",
            "Fit an active coolant loop so the pack runs cooler, at higher unit cost.",
            [
                Intervention(type=InterventionType.SET_VALUE, target=obs.id,
                             value="55", unit="degC",
                             note="active cooling brings peak pack temperature down"),
                Intervention(type=InterventionType.ASSERT,
                             subject="Thermal Architecture A7", predicate="cost",
                             value="96000", unit="INR",
                             note="active loop adds cost per vehicle"),
            ],
        ))

    if phase2 is not None and kirin is not None and volta_cap is not None:
        scenarios.append((
            "B: dual-source the cells",
            "Move part of the allocation to Kirin, whose cells are rated below 60 degC.",
            [
                Intervention(type=InterventionType.ADD_DEPENDENCY,
                             source=phase2.id, target=kirin.id,
                             edge_type="DEPENDS_ON",
                             note="second qualified cell source"),
                Intervention(type=InterventionType.ASSERT,
                             subject="Kirin Energy Systems", predicate="capacity",
                             value="34000", unit="units per month",
                             note="allocation moved to the second source"),
            ],
        ))

    if arch is not None:
        scenarios.append((
            "C: delay deployment",
            "Hold revenue service until the thermal case is re-certified.",
            [
                Intervention(type=InterventionType.ASSERT,
                             subject="Fleet Deployment Phase 2", predicate="duration",
                             value="15", unit="months",
                             note="six-month slip to revenue service"),
                Intervention(type=InterventionType.INVALIDATE, target=arch.id,
                             note="passive design withdrawn pending redesign"),
            ],
        ))

    results = []
    for name, desc, ivs in scenarios:
        r = simulator.simulate(store, ivs, name=name, description=desc)
        results.append(r)
        print(f"\n  {name}")
        print(f"    {desc}")
        for line in r.interventions:
            print(f"      intervention: {line}")
        moved = [d for d in r.deltas if d.direction in ("better", "worse")]
        for d in moved[:6]:
            print(f"      {d.name:<24} {str(d.before):>10} -> {str(d.after):<10} "
                  f"[{d.direction}]")
        print(f"      first-order effects: {len(r.first_order)}, "
              f"second-order: {len(r.second_order)}")

    if len(results) >= 2:
        rule("7. EXPLAIN - recommended course of action")
        cmp = simulator.compare(results)
        for row in cmp["ranking"]:
            print(f"\n  score {row['score']:>7.2f}  {row['scenario']}")
            print(f"    {row['verdict']}")
        print(f"\n  RECOMMENDED: {cmp['recommended']}")
        print(f"  {cmp['rationale']}")

    # ---------------------------------------------------------------- 8
    rule("8. ACTIVE VALIDATION - which unknown is worth resolving first")
    q = build_queue(store, limit=5)
    print(f"  {q.summary}\n")
    for c in q.candidates:
        print(f"    {c.id:<6} {c.label[:40]:<42} "
              f"reduces uncertainty {c.uncertainty_reduction_pct:>5.1f}%")
        print(f"           {c.suggested_action}")

    # ---------------------------------------------------------------- 9
    rule("9. WRITE BACK - Markdown projection")
    from backend.core.config import settings
    from backend.vault.markdown_writer import project_all

    n = project_all(store)
    print(f"  {n} notes written to {settings.vault_dir}")
    health_line(store, "final health")

    rule()
    print("Demo complete. Start the API with:  python -m backend.main")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
