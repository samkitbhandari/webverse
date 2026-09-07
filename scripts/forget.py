"""Remove knowledge that should not be there.

Deleting a file from ``incoming_files/`` *retires* what it supported -- the
claims survive at lower confidence, because knowledge that was true because of
a document does not become false when the document is moved. That is the right
default, and it is not what you want after a bad ingest or a demo rehearsal.

This erases instead.

    python scripts/forget.py                           # what is in the graph
    python scripts/forget.py --find "Volta Cells Ltd capacity"
    python scripts/forget.py --node C19 C20 C21        # erase specific nodes
    python scripts/forget.py --source "Supplier Notice"  # erase a whole source
    python scripts/forget.py --node C19 --yes          # skip the confirmation

Kuzu allows a single writer, so this talks to the running API when there is one
and opens the graph directly when there is not. Either way it does the same
thing -- you never have to stop the server to tidy up.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

API = "http://127.0.0.1:8000"


# ---------------------------------------------------------------------------
# Two ways to reach the graph
# ---------------------------------------------------------------------------
class ViaApi:
    mode = "running server"

    def __init__(self) -> None:
        import httpx

        self.c = httpx.Client(base_url=API, timeout=30.0)

    def nodes(self, q: str | None = None) -> list[dict]:
        r = self.c.get("/api/nodes", params={"limit": 1000, **({"q": q} if q else {})})
        r.raise_for_status()
        return r.json()["items"]

    def sources(self) -> list[dict]:
        return self.c.get("/api/sources").json()["sources"]

    def forget(self, node_id: str) -> dict:
        r = self.c.delete(f"/api/nodes/{node_id}")
        r.raise_for_status()
        return r.json()

    def purge(self, path: str) -> dict:
        r = self.c.delete("/api/sources", params={"path": path})
        r.raise_for_status()
        return r.json()


class ViaStore:
    mode = "local graph"

    def __init__(self) -> None:
        from backend.graph.store import get_store
        from backend.pipeline import Pipeline

        self.store = get_store()
        self.pipe = Pipeline(store=self.store)

    @staticmethod
    def _row(n) -> dict:
        return {
            "id": n.id, "type": n.type.value, "status": n.status.value,
            "label": n.label, "value": n.value.render() if n.value else None,
            "provenance": [{"source": p.source_label or p.source_path,
                            "path": p.source_path} for p in n.provenance],
        }

    def nodes(self, q: str | None = None) -> list[dict]:
        items = self.store.nodes.values()
        if q:
            needle = q.lower()
            items = [n for n in items
                     if needle in n.label.lower() or needle in (n.subject or "").lower()]
        return [self._row(n) for n in items]

    def sources(self) -> list[dict]:
        counts: dict[str, dict[str, Any]] = {}
        for n in self.store.nodes.values():
            for p in n.provenance:
                if not p.source_path:
                    continue
                row = counts.setdefault(p.source_path, {
                    "path": p.source_path, "label": p.source_label,
                    "nodes": 0, "sole_evidence": 0,
                })
                row["nodes"] += 1
                if len({q.source_path for q in n.provenance}) == 1:
                    row["sole_evidence"] += 1
        return sorted(counts.values(), key=lambda r: -r["nodes"])

    def forget(self, node_id: str) -> dict:
        return self.pipe.forget_node(node_id)

    def purge(self, path: str) -> dict:
        return self.pipe.purge_source(path)


def connect():
    try:
        import httpx

        if httpx.get(f"{API}/health", timeout=1.5).status_code == 200:
            return ViaApi()
    except Exception:
        pass
    return ViaStore()


# ---------------------------------------------------------------------------
def show(rows: list[dict]) -> None:
    for r in sorted(rows, key=lambda r: r["id"]):
        src = r["provenance"][0]["source"] if r["provenance"] else "-"
        print(f"  {r['id']:<6} {r['type']:<12} {r['status']:<12} "
              f"{(r['value'] or ''):<22} {r['label'][:42]:<44} <- {src}")


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--find", metavar="TEXT", help="search labels and subjects")
    ap.add_argument("--node", nargs="+", metavar="ID", help="node ids to erase")
    ap.add_argument("--source", metavar="PATH_OR_LABEL",
                    help="erase everything a source is solely responsible for")
    ap.add_argument("--yes", action="store_true", help="skip the confirmation")
    args = ap.parse_args()

    g = connect()
    print(f"[{g.mode}]")

    # --- overview (default) ---------------------------------------------
    if not any([args.find, args.node, args.source]):
        srcs = g.sources()
        print(f"\n{len(srcs)} source(s) in the graph:\n")
        for s in srcs:
            print(f"  {s['nodes']:>4} nodes ({s['sole_evidence']:>3} sole evidence)  "
                  f"{s['label'] or ''}")
            print(f"       {s['path']}")
        print("\nPass --find to search, --node or --source to erase.")
        return 0

    # --- search -----------------------------------------------------------
    if args.find:
        hits = g.nodes(args.find)
        if not hits:
            print(f"nothing matches {args.find!r}")
            return 1
        print(f"\n{len(hits)} match(es):\n")
        show(hits)
        ids = " ".join(sorted(h["id"] for h in hits))
        print(f"\nErase them with:\n  python scripts/forget.py --node {ids}")
        return 0

    # --- erase a source ---------------------------------------------------
    if args.source:
        needle = args.source.lower()
        matches = {s["path"] for s in g.sources()
                   if needle in s["path"].lower() or needle in (s["label"] or "").lower()}
        if not matches:
            print(f"no source matches {args.source!r}. Run with no arguments to list them.")
            return 1
        if len(matches) > 1:
            print("that matches several sources; be more specific:")
            for m in sorted(matches):
                print(f"  {m}")
            return 1

        path = matches.pop()
        doomed = [n for n in g.nodes()
                  if {p["path"] for p in n["provenance"]} == {path}]
        print(f"\nErasing everything solely from:\n  {path}\n")
        show(doomed)
        if not args.yes and input(f"\nerase {len(doomed)} node(s)? [y/N] ").lower() != "y":
            print("cancelled")
            return 1
        r = g.purge(path)
        print(f"\nremoved {len(r['removed'])} node(s); "
              f"kept {len(r['kept_with_other_evidence'])} with other evidence")
        return 0

    # --- erase specific nodes --------------------------------------------
    known = {n["id"]: n for n in g.nodes()}
    missing = [i for i in args.node if i not in known]
    if missing:
        print(f"unknown node(s): {', '.join(missing)}")
        return 1

    print()
    show([known[i] for i in args.node])
    if not args.yes and input(f"\nerase {len(args.node)} node(s)? [y/N] ").lower() != "y":
        print("cancelled")
        return 1
    for nid in args.node:
        r = g.forget(nid)
        print(f"  removed {r['id']} ({r['edges_removed']} relationship(s))")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
