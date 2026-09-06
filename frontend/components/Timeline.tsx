"use client";

import clsx from "clsx";
import type { Timeline as TimelineData } from "@/lib/types";
import { STATUS_COLOR } from "@/lib/theme";
import { Empty, StatusChip } from "./ui";

const fmt = (iso: string | null) =>
  iso ? new Date(iso).toISOString().slice(0, 10) : null;

/**
 * The value history of one property. Superseded entries stay visible with a
 * closed validity window -- the point of a temporal graph is that old beliefs
 * are retired, not deleted.
 */
export function Timeline({
  timeline, onSelect,
}: {
  timeline: TimelineData | null;
  onSelect?: (id: string) => void;
}) {
  if (!timeline || timeline.points.length === 0) {
    return <Empty>No temporal history for this property.</Empty>;
  }

  return (
    <div className="scroll-y h-full p-3">
      <div className="mb-2 text-[11px] text-ink-400">
        <span className="text-ink-200">{timeline.subject}</span>
        <span className="mx-1.5 text-ink-600">/</span>
        <span className="text-ink-300">{timeline.predicate}</span>
      </div>

      <ol className="relative space-y-3 border-l border-ink-700 pl-4">
        {timeline.points.map((p) => {
          const color = STATUS_COLOR[p.status] ?? "#8792a5";
          const retired = p.status === "SUPERSEDED" || p.status === "INVALIDATED";
          return (
            <li key={p.node_id} className="relative">
              <span
                className="absolute -left-[21px] top-1.5 h-2.5 w-2.5 rounded-full border-2 border-ink-950"
                style={{ backgroundColor: color }}
              />
              <button
                onClick={() => onSelect?.(p.node_id)}
                className={clsx(
                  "w-full rounded-md border border-ink-700/60 bg-ink-850/60 px-2.5 py-2 text-left transition hover:border-ink-600",
                  retired && "opacity-60",
                )}
              >
                <div className="flex items-center justify-between gap-2">
                  <span className={clsx("metric text-sm", retired && "line-through decoration-ink-500")}>
                    {p.value ?? "—"}
                  </span>
                  <StatusChip status={p.status} />
                </div>
                <div className="mt-1 flex items-center gap-2 text-[10px] text-ink-400">
                  <span className="metric">{p.node_id}</span>
                  <span>
                    {fmt(p.valid_from) ?? "—"} → {fmt(p.valid_until) ?? "present"}
                  </span>
                  <span className="metric ml-auto">c={p.confidence.toFixed(2)}</span>
                </div>
                {p.source && (
                  <div className="mt-0.5 truncate text-[10px] text-ink-500">{p.source}</div>
                )}
              </button>
            </li>
          );
        })}
      </ol>
    </div>
  );
}
