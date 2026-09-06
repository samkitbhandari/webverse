"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { Radar, Search, Zap } from "lucide-react";
import { ImpactPanel } from "@/components/ImpactPanel";
import { KnowledgeGraph, type GraphHighlight } from "@/components/KnowledgeGraph";
import { NodeInspector } from "@/components/NodeInspector";
import { useStream } from "@/components/Shell";
import { Empty, ErrorNote, Panel, Spinner, StatusChip, TypeChip } from "@/components/ui";
import { api } from "@/lib/api";
import { useLiveData } from "@/lib/websocket";
import type { GraphPayload, ImpactReport, NodeDetail } from "@/lib/types";

/** Node types that make sense as the origin of a change. */
const SEED_TYPES = "Event,Claim,Observation,Requirement,Constraint,Assumption";

export default function ImpactsPage() {
  const { lastChange } = useStream();
  const [query, setQuery] = useState("");
  const [seed, setSeed] = useState<string | null>(null);
  const [depth, setDepth] = useState(6);
  const [report, setReport] = useState<ImpactReport | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [path, setPath] = useState<string[] | undefined>();
  const [inspect, setInspect] = useState<string | null>(null);

  const candidates = useLiveData(
    () => api.nodes({ type: SEED_TYPES, q: query || undefined, limit: 60 }),
    [lastChange, query],
  );
  const graph = useLiveData<GraphPayload>(() => api.graph({ limit: 400 }), [lastChange]);
  const detail = useLiveData<NodeDetail | null>(
    () => (inspect ? api.node(inspect) : Promise.resolve(null)),
    [inspect, lastChange],
  );

  const run = useCallback(async (id: string, d: number) => {
    setBusy(true);
    setError(null);
    setPath(undefined);
    try {
      setReport(await api.impact([id], { max_depth: d }));
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      setReport(null);
    } finally {
      setBusy(false);
    }
  }, []);

  useEffect(() => {
    if (seed) run(seed, depth);
  }, [seed, depth, run]);

  const highlight: GraphHighlight = useMemo(() => {
    const impacted: Record<string, number> = {};
    for (const a of report?.affected ?? []) impacted[a.id] = a.severity;
    if (seed) impacted[seed] = Math.max(impacted[seed] ?? 0, 1);
    return { impacted, path };
  }, [report, seed, path]);

  return (
    <div className="grid h-full grid-cols-12 gap-2 p-2">
      <Panel
        className="col-span-12 lg:col-span-3"
        title="Change origin"
        actions={<span className="text-[10px] text-ink-500">{candidates.data?.total ?? 0}</span>}
      >
        <div className="flex h-full min-h-0 flex-col">
          <div className="shrink-0 border-b border-ink-800/70 p-2">
            <div className="flex items-center gap-1.5 rounded-md border border-ink-700 bg-ink-900 px-2 py-1.5">
              <Search size={12} className="shrink-0 text-ink-500" />
              <input
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                placeholder="filter claims, events, requirements"
                className="w-full bg-transparent text-xs text-ink-100 outline-none placeholder:text-ink-600"
              />
            </div>
            <div className="mt-2 flex items-center gap-2">
              <span className="text-[10px] text-ink-400">depth</span>
              <input
                type="range" min={1} max={8} value={depth}
                onChange={(e) => setDepth(Number(e.target.value))}
                className="flex-1 accent-signal"
              />
              <span className="metric w-4 text-[11px] text-ink-300">{depth}</span>
            </div>
          </div>

          {candidates.data ? (
            <ul className="scroll-y min-h-0 flex-1 divide-y divide-ink-800/70">
              {candidates.data.items.map((n) => (
                <li key={n.id}>
                  <button
                    onClick={() => setSeed(n.id)}
                    className={`row-hover w-full px-2.5 py-2 text-left ${
                      seed === n.id ? "bg-signal/10" : ""
                    }`}
                  >
                    <span className="flex items-center gap-1.5">
                      <TypeChip type={n.type} />
                      <StatusChip status={n.status} />
                    </span>
                    <span className="mt-1 block text-xs leading-snug text-ink-100">{n.label}</span>
                  </button>
                </li>
              ))}
              {candidates.data.items.length === 0 && <Empty>Nothing matches that filter.</Empty>}
            </ul>
          ) : (
            <Spinner />
          )}
        </div>
      </Panel>

      <Panel
        className="col-span-12 lg:col-span-5"
        title={
          <span className="flex items-center gap-1.5">
            <Radar size={12} /> Propagation
          </span>
        }
        actions={
          seed && (
            <button className="btn" onClick={() => run(seed, depth)} disabled={busy}>
              <Zap size={12} /> recompute
            </button>
          )
        }
      >
        {error && <ErrorNote message={error} />}
        {busy && <Spinner label="propagating through the dependency graph" />}
        {!busy && !seed && (
          <Empty>
            Pick a claim, event or requirement on the left. NEXUS will trace every belief and
            decision that depends on it, with the path that carries the change.
          </Empty>
        )}
        {!busy && seed && (
          <ImpactPanel report={report} onSelect={setInspect} onFocusPath={setPath} />
        )}
      </Panel>

      <div className="col-span-12 flex flex-col gap-2 lg:col-span-4">
        <Panel className="min-h-0 flex-[3]" title="Blast radius" bodyClassName="relative">
          <KnowledgeGraph
            data={graph.data}
            selected={seed}
            highlight={highlight}
            onSelect={(id) => id && setInspect(id)}
          />
        </Panel>
        <Panel className="min-h-0 flex-[2]" title="Inspector">
          <NodeInspector
            node={detail.data ?? null}
            loading={detail.loading}
            onSelect={setInspect}
            onImpact={(id) => setSeed(id)}
          />
        </Panel>
      </div>
    </div>
  );
}
