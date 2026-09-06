"use client";

import { useCallback, useMemo, useState } from "react";
import { useRouter } from "next/navigation";
import { Activity, FilePlus2, RefreshCw } from "lucide-react";
import { KnowledgeGraph, type GraphHighlight } from "@/components/KnowledgeGraph";
import { KnowledgeHealthPanel } from "@/components/KnowledgeHealth";
import { ContradictionPanel } from "@/components/ContradictionPanel";
import { EventFeed } from "@/components/EventFeed";
import { useStream } from "@/components/Shell";
import { Empty, ErrorNote, Panel, Spinner } from "@/components/ui";
import { api } from "@/lib/api";
import { useLiveData } from "@/lib/websocket";
import type { Contradiction, GraphPayload, KnowledgeHealth, ValidationQueue } from "@/lib/types";

export default function OverviewPage() {
  const { events, lastChange } = useStream();
  const router = useRouter();
  const [selected, setSelected] = useState<string | null>(null);

  const graph = useLiveData<GraphPayload>(() => api.graph({ limit: 400 }), [lastChange]);
  const health = useLiveData<KnowledgeHealth>(() => api.health(), [lastChange]);
  const contradictions = useLiveData<{ count: number; items: Contradiction[] }>(
    () => api.contradictions(), [lastChange],
  );
  const validation = useLiveData<ValidationQueue>(() => api.validationQueue(6), [lastChange]);

  /** Nodes touched by recent events get a ring; contradicted nodes get a red edge. */
  const highlight: GraphHighlight = useMemo(() => {
    const recent = new Set<string>();
    const impacted: Record<string, number> = {};
    for (const e of events.slice(0, 40)) {
      const id = e.payload?.id;
      if (typeof id === "string") recent.add(id);
      if (e.event_type === "IMPACT_COMPUTED") {
        for (const a of e.payload?.top ?? []) {
          impacted[a.id] = Math.max(impacted[a.id] ?? 0, a.severity ?? 0);
        }
      }
    }
    const contradicted = new Set<string>();
    for (const c of contradictions.data?.items ?? []) {
      contradicted.add(c.left);
      contradicted.add(c.right);
    }
    return { recent, impacted, contradicted };
  }, [events, contradictions.data]);

  const inspect = useCallback(
    (id: string | null) => {
      setSelected(id);
      if (id) router.push(`/graph?focus=${encodeURIComponent(id)}`);
    },
    [router],
  );

  return (
    <div className="grid h-full grid-cols-12 grid-rows-[minmax(0,1.35fr)_minmax(0,1fr)] gap-2 p-2">
      <Panel
        className="col-span-12 row-span-2 lg:col-span-8"
        title={
          <span className="flex items-center gap-2">
            Living knowledge graph
            {graph.data && (
              <span className="metric font-normal normal-case tracking-normal text-ink-500">
                {graph.data.nodes.length} shown / {graph.data.stats.nodes} total
              </span>
            )}
          </span>
        }
        actions={
          <div className="flex items-center gap-1">
            <button className="btn" onClick={graph.reload} title="Refetch graph">
              <RefreshCw size={12} />
            </button>
          </div>
        }
        bodyClassName="relative"
      >
        {graph.error && <ErrorNote message={graph.error} />}
        {graph.loading && !graph.data && <Spinner label="loading graph" />}
        <KnowledgeGraph
          data={graph.data}
          selected={selected}
          highlight={highlight}
          onSelect={inspect}
        />
        <div className="pointer-events-none absolute bottom-2 left-2 rounded bg-ink-950/80 px-2 py-1 text-[10px] text-ink-500">
          click a node to open it in the graph explorer
        </div>
      </Panel>

      <Panel
        className="col-span-12 lg:col-span-4"
        title="Knowledge health"
        actions={
          <a href="/entities" className="text-[11px] text-ink-400 hover:text-signal">
            browse →
          </a>
        }
      >
        {health.error && <ErrorNote message={health.error} />}
        {health.data ? <KnowledgeHealthPanel health={health.data} /> : <Spinner />}
      </Panel>

      <Panel
        className="col-span-12 lg:col-span-4"
        title={
          <span className="flex items-center gap-1.5">
            <Activity size={12} /> Live activity
          </span>
        }
        actions={
          <span className="text-[10px] text-ink-500">{events.length} events</span>
        }
      >
        <EventFeed events={events} />
      </Panel>

      <Panel
        className="col-span-12 lg:col-span-4"
        title={`Contradictions (${contradictions.data?.count ?? 0})`}
      >
        {contradictions.error && <ErrorNote message={contradictions.error} />}
        {contradictions.data ? (
          <ContradictionPanel items={contradictions.data.items} onSelect={inspect} />
        ) : (
          <Spinner />
        )}
      </Panel>

      <Panel
        className="col-span-12 lg:col-span-4"
        title="What to validate next"
        actions={
          validation.data && (
            <span className="metric text-[10px] text-ink-500">
              {validation.data.open_questions} open
            </span>
          )
        }
      >
        {validation.data ? (
          validation.data.candidates.length === 0 ? (
            <Empty>{validation.data.summary}</Empty>
          ) : (
            <ul className="scroll-y h-full divide-y divide-ink-800/70">
              {validation.data.candidates.map((c, i) => (
                <li key={c.id}>
                  <button
                    onClick={() => inspect(c.id)}
                    className="row-hover flex w-full items-start gap-2 px-3 py-2 text-left"
                  >
                    <span className="metric mt-0.5 w-5 shrink-0 text-[11px] text-ink-500">
                      {i + 1}
                    </span>
                    <span className="min-w-0 flex-1">
                      <span className="block truncate text-xs text-ink-100">{c.label}</span>
                      <span className="mt-0.5 block text-[10px] text-ink-400">
                        {c.suggested_action}
                      </span>
                    </span>
                    <span className="metric shrink-0 text-[11px] text-signal">
                      −{c.uncertainty_reduction_pct.toFixed(0)}%
                    </span>
                  </button>
                </li>
              ))}
            </ul>
          )
        ) : (
          <Spinner />
        )}
      </Panel>
    </div>
  );
}
