"""Print the evaluation scorecard.

    python scripts/evaluate.py            # the scorecard
    python scripts/evaluate.py --verbose  # plus every miss and false positive

This is the answer to "how do you know it works?". It scores NEXUS against
human-written labels and, on the one task that matters, against a retrieval
baseline given the same chunks and the same embedder.
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import yaml

W = 78


def rule(title: str = "") -> None:
    print(f"\n{'=' * W}\n{title}\n{'=' * W}" if title else "-" * W)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--verbose", action="store_true", help="list every miss")
    args = ap.parse_args()
    logging.disable(logging.INFO)

    from evaluation.baseline import evaluate_baseline
    from evaluation.harness import GOLD, run

    gold = yaml.safe_load(GOLD.read_text("utf-8"))

    rule("NEXUS OMEGA - EVALUATION SCORECARD")
    print("  Labels were written by reading the documents, not by recording what")
    print("  the system outputs. Misses below are real gaps, not tuning targets.")

    result = run(verbose=args.verbose)
    scores = result["scores"]

    rule("1. EXTRACTION  (can it read a document correctly?)")
    print(f"  compiler: {result['extractor']}    "
          f"graph: {result['nodes']} nodes / {result['edges']} relationships\n")
    for s in scores[:2]:
        print(s.row())

    rule("2. REASONING  (does it understand what changed?)")
    for s in scores[2:4]:
        print(s.row())

    rule("3. DECISION IMPACT  (the task retrieval cannot do)")
    impact = scores[4]
    print(impact.row())
    if result["affected"]:
        print("\n  decisions identified as no longer supported:")
        for a in result["affected"]:
            print(f"    severity {a.severity:.3f}  {a.label[:56]}")
            print(f"      because: {a.explanation[:140]}")

    # ----------------------------------------------------------------- baseline
    rule("4. BASELINE COMPARISON  (same chunks, same embedder, top-10)")
    corpus = [ROOT / d["path"] for d in gold["documents"]]
    expected = gold["reasoning"]["expected_affected_decisions"]
    base = evaluate_baseline(corpus, gold["reasoning"]["question"], expected)

    print(f"  retrieval indexed {base['chunks_indexed']} chunks, "
          f"returned top {base['chunks_retrieved']}")
    print(f"  found the regulation text:        "
          f"{'yes' if base['regulation_retrieved'] else 'no'}"
          "   <- retrieval is good at this")
    print(f"  named the affected decisions:     "
          f"{len(base['decisions_found'])}/{len(expected)} "
          f"({base['recall']:.0%} recall, scored generously)")
    print(f"  explained WHY each is affected:   no")
    print(f"  ranked them by severity:          no")
    print(f"  traversed the dependency chain:   no")

    print(f"\n  NEXUS on the same question:       "
          f"{impact.tp}/{impact.tp + impact.fn} "
          f"({impact.recall:.0%} recall), with the causal path for each")

    # ----------------------------------------------------------------- summary
    rule("SUMMARY")
    for s in scores:
        bar = "#" * int(s.f1 * 28)
        print(f"  {s.name:<26} {s.f1:5.1%}  {bar}")

    if args.verbose:
        rule("MISSES AND FALSE POSITIVES")
        for s in scores:
            if s.misses or s.spurious:
                print(f"\n  {s.name}")
                for m in s.misses:
                    print(f"    MISSED    {m}")
                for f in s.spurious[:12]:
                    print(f"    SPURIOUS  {f}")
                if len(s.spurious) > 12:
                    print(f"    ... and {len(s.spurious) - 12} more")

    print()
    weakest = min(scores, key=lambda s: s.f1)
    print(f"  Weakest link: {weakest.name} at {weakest.f1:.0%} F1.")
    print("  Run with --verbose to see exactly what it missed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
