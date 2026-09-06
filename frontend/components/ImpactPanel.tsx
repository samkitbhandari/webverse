"use client";

import clsx from "clsx";
import { useState } from "react";
import { ChevronDown, GitBranch, Radar } from "lucide-react";
import type { ImpactReport, ImpactedNode } from "@/lib/types";
import { TYPE_COLOR } from "@/lib/theme";
import { Empty, PathChain, TypeChip } from "./ui";

function severityTone(s: number): string {
  if (s >= 0.3) return "#ef476f";
  if (s >= 0.15) return "#f4a261";
  if (s >= 0.05) return "#facc15";
  return "#5b6779";
}

function Row({
  node, expanded, onToggle, onSelect, onFocusPath,
}: {
  node: ImpactedNode;
  expanded: boolean;
  onToggle: () => void;
  onSelect?: (id: string) => void;
  onFocusPath?: (path: string[]) => void;
}) {
  const tone = severityTone(node.severity);
  return (
    <li>
      <button onClick={onToggle} className="row-hover flex w-full items-center gap-2 px-3 py-1.5 text-left">
        <span className="metric w-10 shrink-0 text-right text-[11px]" style={{ color: tone }}>
          {node.severity.toFixed(3)}
        </span>
        <span className="h-6 w-1 shrink-0 rounded-full" style={{ backgroundColor: tone }} />
        <span className="min-w-0 flex-1">
          <span className="block truncate text-xs text-ink-100">{node.label}</span>
          <span className="mt-0.5 flex items-center gap-1.5 text-[10px] text-ink-400">
            <TypeChip type={node.type} />
            <span>{node.depth} hop{node.depth === 1 ? "" : "s"}</span>
            <span>· score {node.score.toFixed(3)}</span>
          </span>
        </span>
        <ChevronDown
          size={13}
          className={clsx("shrink-0 text-ink-500 transition-transform", expanded && "rotate-180")}
        />
      </button>

      {expanded && (
        <div className="space-y-2 border-t border-ink-800/70 bg-ink-850/50 px-3 py-2">
          <div className="text-[11px] leading-relaxed">
            <PathChain text={node.explanation} />
          </div>
          <ol className="space-y-1">
            {node.hops.map((hop, i) => (
              <li key={i} className="flex items-center gap-2 text-[10px] text-ink-400">
                <span className="metric w-4 text-ink-500">{i + 1}</span>
                <span className="truncate text-ink-300">{hop.from_label}</span>
                <span className="shrink-0 rounded bg-ink-800 px-1 font-mono uppercase text-ink-400">
                  {hop.edge_type}
                </span>
                <span className="truncate text-ink-300">{hop.to_label}</span>
                <span className="metric ml-auto shrink-0">{hop.influence.toFixed(2)}</span>
              </li>
            ))}
          </ol>
          <div className="flex gap-2">
            <button className="btn" onClick={() => onSelect?.(node.id)}>
              Inspect node
            </button>
            {node.path.length > 1 && (
              <button className="btn" onClick={() => onFocusPath?.(node.path)}>
                <GitBranch size={12} /> Trace on graph
              </button>
            )}
          </div>
        </div>
      )}
    </li>
  );
}

export function ImpactPanel({
  report, onSelect, onFocusPath,
}: {
  report: ImpactReport | null;
  onSelect?: (id: string) => void;
  onFocusPath?: (path: string[]) => void;
}) {
  const [open, setOpen] = useState<string | null>(null);

  if (!report) {
    return <Empty>Select a node and run impact analysis to see what depends on it.</Empty>;
  }
  if (report.total_affected === 0) {
    return (
      <Empty>
        {report.notes[0] ?? "Nothing downstream depends on this node."}
      </Empty>
    );
  }

  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="flex shrink-0 items-center gap-3 border-b border-ink-800/70 px-3 py-2 text-[11px]">
        <Radar size={13} className="text-signal" />
        <span className="text-ink-300">
          from <span className="text-ink-100">{report.seed_labels.join(", ")}</span>
        </span>
        <span className="metric ml-auto text-ink-400">
          {report.total_affected} affected · {report.affected_decisions.length} decision
          {report.affected_decisions.length === 1 ? "" : "s"} · depth {report.max_depth_reached}
        </span>
      </div>

      {report.affected_decisions.length > 0 && (
        <div className="shrink-0 border-b border-ink-800/70 bg-danger/5 px-3 py-2">
          <div className="mb-1 text-[10px] uppercase tracking-wider text-danger">
            Decisions requiring revalidation
          </div>
          <ul className="space-y-0.5">
            {report.affected_decisions.map((d) => (
              <li key={d.id}>
                <button
                  onClick={() => onSelect?.(d.id)}
                  className="flex w-full items-center gap-2 text-left text-[11px] text-ink-200 hover:text-white"
                >
                  <span className="metric shrink-0" style={{ color: TYPE_COLOR.Decision }}>
                    {d.severity.toFixed(3)}
                  </span>
                  <span className="truncate">{d.label}</span>
                </button>
              </li>
            ))}
          </ul>
        </div>
      )}

      <ul className="scroll-y min-h-0 flex-1 divide-y divide-ink-800/70">
        {report.affected.map((n) => (
          <Row
            key={n.id}
            node={n}
            expanded={open === n.id}
            onToggle={() => setOpen(open === n.id ? null : n.id)}
            onSelect={onSelect}
            onFocusPath={onFocusPath}
          />
        ))}
      </ul>
    </div>
  );
}
