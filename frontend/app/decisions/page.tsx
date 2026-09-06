"use client";

import { useEffect, useState } from "react";
import { AlertTriangle, GitBranch } from "lucide-react";
import { DecisionLineage } from "@/components/DecisionLineage";
import { ImpactPanel } from "@/components/ImpactPanel";
import { NodeInspector } from "@/components/NodeInspector";
import { useStream } from "@/components/Shell";
import { Empty, ErrorNote, Panel, Spinner, StatusChip } from "@/components/ui";
import { api } from "@/lib/api";
import { useLiveData } from "@/lib/websocket";
import type { NodeDetail } from "@/lib/types";

export default function DecisionsPage() {
  const { lastChange } = useStream();
  const [selected, setSelected] = useState<string | null>(null);
  const [inspect, setInspect] = useState<string | null>(null);

  const list = useLiveData(() => api.nodes({ type: "Decision", limit: 100 }), [lastChange]);
  const decision = useLiveData<NodeDetail | null>(
    () => (selected ? api.decision(selected) : Promise.resolve(null)),
    [selected, lastChange],
  );
  const inspected = useLiveData<NodeDetail | null>(
    () => (inspect ? api.node(inspect) : Promise.resolve(null)),
    [inspect, lastChange],
  );

  useEffect(() => {
    if (!selected && list.data?.items.length) setSelected(list.data.items[0].id);
  }, [list.data, selected]);

  return (
    <div className="grid h-full grid-cols-12 gap-2 p-2">
      <Panel
        className="col-span-12 lg:col-span-3"
        title={
          <span className="flex items-center gap-1.5">
            <GitBranch size={12} /> Decisions
          </span>
        }
        actions={<span className="metric text-[10px] text-ink-500">{list.data?.total ?? 0}</span>}
      >
        {list.error && <ErrorNote message={list.error} />}
        {list.data ? (
          list.data.items.length === 0 ? (
            <Empty>No decisions in the graph yet.</Empty>
          ) : (
            <ul className="scroll-y h-full divide-y divide-ink-800/70">
              {list.data.items.map((d) => (
                <li key={d.id}>
                  <button
                    onClick={() => setSelected(d.id)}
                    className={`row-hover w-full px-2.5 py-2 text-left ${
                      selected === d.id ? "bg-signal/10" : ""
                    }`}
                  >
                    <span className="flex items-center gap-1.5">
                      <span className="metric text-[10px] text-ink-500">{d.id}</span>
                      <StatusChip status={d.status} />
                    </span>
                    <span className="mt-1 block text-xs leading-snug text-ink-100">{d.label}</span>
                  </button>
                </li>
              ))}
            </ul>
          )
        ) : (
          <Spinner />
        )}
      </Panel>

      <Panel className="col-span-12 lg:col-span-5" title="Decision lineage">
        {decision.loading && <Spinner label="tracing lineage" />}
        {decision.error && <ErrorNote message={decision.error} />}
        {!decision.loading && (
          <DecisionLineage lineage={decision.data?.lineage ?? null} onSelect={setInspect} />
        )}
      </Panel>

      <div className="col-span-12 flex flex-col gap-2 lg:col-span-4">
        <Panel
          className="min-h-0 flex-1"
          title={
            <span className="flex items-center gap-1.5">
              <AlertTriangle size={12} /> If this decision changed
            </span>
          }
        >
          <ImpactPanel
            report={decision.data?.impact_if_changed ?? null}
            onSelect={setInspect}
          />
        </Panel>
        <Panel className="min-h-0 flex-1" title="Inspector">
          <NodeInspector
            node={inspected.data ?? null}
            loading={inspected.loading}
            onSelect={setInspect}
          />
        </Panel>
      </div>
    </div>
  );
}
