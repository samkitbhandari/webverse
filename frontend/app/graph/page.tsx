"use client";

import { Suspense, useCallback, useEffect, useMemo, useState } from "react";
import { useSearchParams } from "next/navigation";
import { Crosshair, Filter, Layers, X } from "lucide-react";
import { KnowledgeGraph, type GraphHighlight } from "@/components/KnowledgeGraph";
import { NodeInspector } from "@/components/NodeInspector";
import { ImpactPanel } from "@/components/ImpactPanel";
import { Timeline } from "@/components/Timeline";
import { useStream } from "@/components/Shell";
import { ErrorNote, Panel, Spinner, TypeChip } from "@/components/ui";
import { api } from "@/lib/api";
import { useLiveData } from "@/lib/websocket";
import type { GraphPayload, ImpactReport, NodeDetail, NodeType, Timeline as TL } from "@/lib/types";

const ALL_TYPES: NodeType[] = [
  "Entity", "Claim", "Observation", "Assumption", "Decision",
  "Requirement", "Constraint", "Event", "Risk", "Action", "Evidence", "Concept",
];

function GraphExplorer() {
  const params = useSearchParams();
  const { lastChange } = useStream();
  const [selected, setSelected] = useState<string | null>(params.get("focus"));
  const [types, setTypes] = useState<Set<NodeType>>(new Set(ALL_TYPES));
  const [includeRetired, setIncludeRetired] = useState(true);
  const [focusMode, setFocusMode] = useState(false);
  const [path, setPath] = useState<string[] | undefined>();
  const [side, setSide] = useState<"inspect" | "impact" | "timeline">("inspect");

  const typeParam = useMemo(
    () => (types.size === ALL_TYPES.length ? undefined : [...types].join(",")),
    [types],
  );

  const graph = useLiveData<GraphPayload>(
    () =>
      focusMode && selected
        ? api.graphAround(selected, 2)
        : api.graph({ limit: 500, types: typeParam, include_retired: includeRetired }),
    [lastChange, typeParam, includeRetired, focusMode, focusMode ? selected : ""],
  );

  const detail = useLiveData<NodeDetail | null>(
    () => (selected ? api.node(selected) : Promise.resolve(null)),
    [selected, lastChange],
  );

  const [impact, setImpact] = useState<ImpactReport | null>(null);
  const [impactError, setImpactError] = useState<string | null>(null);
  const [timeline, setTimeline] = useState<TL | null>(null);

  const runImpact = useCallback(async (id: string) => {
    setSide("impact");
    setImpact(null);
    setImpactError(null);
    try {
      setImpact(await api.impact([id]));
    } catch (e) {
      setImpactError(e instanceof Error ? e.message : String(e));
    }
  }, []);

  useEffect(() => {
    if (!selected || side !== "timeline") return;
    api.timeline(selected).then(setTimeline).catch(() => setTimeline(null));
  }, [selected, side]);

  const highlight: GraphHighlight = useMemo(() => {
    const impacted: Record<string, number> = {};
    for (const a of impact?.affected ?? []) impacted[a.id] = a.severity;
    return { impacted, path };
  }, [impact, path]);

  const toggleType = (t: NodeType) => {
    setTypes((prev) => {
      const next = new Set(prev);
      if (next.has(t)) next.delete(t);
      else next.add(t);
      return next.size === 0 ? new Set(ALL_TYPES) : next;
    });
  };

  return (
    <div className="flex h-full min-h-0">
      <div className="flex min-w-0 flex-1 flex-col gap-2 p-2">
        <div className="panel flex shrink-0 flex-wrap items-center gap-2 px-3 py-2">
          <Filter size={12} className="text-ink-500" />
          <div className="flex flex-wrap gap-1">
            {ALL_TYPES.map((t) => (
              <button
                key={t}
                onClick={() => toggleType(t)}
                className={types.has(t) ? "opacity-100" : "opacity-30"}
              >
                <TypeChip type={t} />
              </button>
            ))}
          </div>
          <div className="ml-auto flex items-center gap-1.5">
            <button
              className={`btn ${includeRetired ? "" : "btn-primary"}`}
              onClick={() => setIncludeRetired((v) => !v)}
              title="Hide superseded and invalidated beliefs"
            >
              <Layers size={12} /> {includeRetired ? "all beliefs" : "live only"}
            </button>
            <button
              className={`btn ${focusMode ? "btn-primary" : ""}`}
              onClick={() => setFocusMode((v) => !v)}
              disabled={!selected}
              title="Show only the neighbourhood of the selected node"
            >
              <Crosshair size={12} /> neighbourhood
            </button>
            {path && (
              <button className="btn" onClick={() => setPath(undefined)}>
                <X size={12} /> clear trace
              </button>
            )}
          </div>
        </div>

        <Panel className="min-h-0 flex-1" bodyClassName="relative">
          {graph.error && <ErrorNote message={graph.error} />}
          {graph.loading && !graph.data && <Spinner label="loading graph" />}
          <KnowledgeGraph
            data={graph.data}
            selected={selected}
            highlight={highlight}
            onSelect={(id) => {
              setSelected(id);
              setSide("inspect");
              setPath(undefined);
            }}
          />
          {graph.data?.truncated && (
            <div className="pointer-events-none absolute right-2 top-2 rounded bg-warn/15 px-2 py-1 text-[10px] text-warn">
              view truncated — narrow the filters
            </div>
          )}
        </Panel>
      </div>

      <aside className="flex w-[380px] shrink-0 flex-col gap-2 border-l border-ink-800/70 p-2">
        <div className="flex shrink-0 gap-1">
          {(["inspect", "impact", "timeline"] as const).map((s) => (
            <button
              key={s}
              onClick={() => setSide(s)}
              disabled={!selected}
              className={`btn flex-1 justify-center capitalize ${side === s ? "btn-primary" : ""}`}
            >
              {s}
            </button>
          ))}
        </div>

        <Panel className="min-h-0 flex-1">
          {side === "inspect" && (
            <NodeInspector
              node={detail.data ?? null}
              loading={detail.loading}
              onSelect={(id) => setSelected(id)}
              onImpact={runImpact}
              onClose={() => setSelected(null)}
            />
          )}
          {side === "impact" && (
            <>
              {impactError && <ErrorNote message={impactError} />}
              {!impact && !impactError && selected && <Spinner label="propagating" />}
              <ImpactPanel
                report={impact}
                onSelect={(id) => {
                  setSelected(id);
                  setSide("inspect");
                }}
                onFocusPath={setPath}
              />
            </>
          )}
          {side === "timeline" && <Timeline timeline={timeline} onSelect={setSelected} />}
        </Panel>
      </aside>
    </div>
  );
}

export default function GraphPage() {
  return (
    <Suspense fallback={<Spinner label="loading" />}>
      <GraphExplorer />
    </Suspense>
  );
}
