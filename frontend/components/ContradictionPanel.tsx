"use client";

import clsx from "clsx";
import { useState } from "react";
import { ChevronDown, Scale } from "lucide-react";
import type { Contradiction, CredibilityBreakdown } from "@/lib/types";
import { Empty } from "./ui";

const KIND_TONE: Record<Contradiction["kind"], string> = {
  CONSTRAINT: "#ef476f",
  NUMERIC: "#f4a261",
  SEMANTIC: "#e879a6",
  TEMPORAL: "#7aa2f7",
};

const AXES: (keyof CredibilityBreakdown)[] = [
  "authority", "recency", "confidence", "corroboration", "specificity", "temporal_validity",
];

/** Side-by-side credibility bars: why one belief outranks the other. */
function Scales({ c }: { c: Contradiction }) {
  return (
    <div className="mt-2 space-y-1">
      {AXES.map((axis) => {
        const l = c.left_credibility[axis];
        const r = c.right_credibility[axis];
        const leftWins = l > r;
        return (
          <div key={axis} className="flex items-center gap-2 text-[10px]">
            <div className="flex h-1.5 w-full flex-1 justify-end overflow-hidden rounded-full bg-ink-800">
              <div
                className={clsx("h-full rounded-full", leftWins ? "bg-signal" : "bg-ink-600")}
                style={{ width: `${l * 100}%` }}
              />
            </div>
            <span className="w-24 shrink-0 text-center text-ink-400">
              {axis.replace("_", " ")}
            </span>
            <div className="h-1.5 w-full flex-1 overflow-hidden rounded-full bg-ink-800">
              <div
                className={clsx("h-full rounded-full", !leftWins ? "bg-signal" : "bg-ink-600")}
                style={{ width: `${r * 100}%` }}
              />
            </div>
          </div>
        );
      })}
      <div className="flex items-center gap-2 pt-1 text-[10px] font-medium">
        <span className="flex-1 text-right text-ink-200">
          {c.left_credibility.total.toFixed(3)}
        </span>
        <span className="w-24 shrink-0 text-center text-ink-500">total</span>
        <span className="flex-1 text-ink-200">{c.right_credibility.total.toFixed(3)}</span>
      </div>
    </div>
  );
}

export function ContradictionPanel({
  items, onSelect,
}: {
  items: Contradiction[];
  onSelect?: (id: string) => void;
}) {
  const [open, setOpen] = useState<string | null>(items[0]?.id ?? null);

  if (items.length === 0) {
    return <Empty>No open contradictions. Every live belief is mutually consistent.</Empty>;
  }

  return (
    <ul className="scroll-y h-full divide-y divide-ink-800/70">
      {items.map((c) => {
        const expanded = open === c.id;
        const tone = KIND_TONE[c.kind];
        return (
          <li key={c.id}>
            <button
              onClick={() => setOpen(expanded ? null : c.id)}
              className="row-hover flex w-full items-start gap-2 px-3 py-2 text-left"
            >
              <span
                className="chip mt-0.5 shrink-0"
                style={{ color: tone, backgroundColor: `${tone}1a`, border: `1px solid ${tone}33` }}
              >
                {c.kind}
              </span>
              <span className="min-w-0 flex-1">
                <span className="block text-xs text-ink-100">{c.description}</span>
                <span className="mt-0.5 block text-[10px] text-ink-400">
                  severity {c.severity.toFixed(2)} · margin {c.margin.toFixed(3)}
                </span>
              </span>
              <ChevronDown
                size={13}
                className={clsx("mt-0.5 shrink-0 text-ink-500 transition-transform", expanded && "rotate-180")}
              />
            </button>

            {expanded && (
              <div className="border-t border-ink-800/70 bg-ink-850/50 px-3 py-2.5">
                <div className="grid grid-cols-2 gap-2 text-[11px]">
                  {([["left", c.left, c.left_label, c.left_value],
                     ["right", c.right, c.right_label, c.right_value]] as const).map(
                    ([side, id, label, value]) => (
                      <button
                        key={side}
                        onClick={() => onSelect?.(id)}
                        className={clsx(
                          "rounded-md border px-2 py-1.5 text-left transition",
                          c.winner === id
                            ? "border-good/40 bg-good/10"
                            : c.winner === null
                              ? "border-warn/30 bg-warn/5"
                              : "border-ink-700 bg-ink-900/60 opacity-70",
                        )}
                      >
                        <div className="flex items-center justify-between gap-1">
                          <span className="metric text-ink-400">{id}</span>
                          {c.winner === id && (
                            <span className="chip bg-good/15 text-good">believed</span>
                          )}
                        </div>
                        <div className="mt-0.5 truncate text-ink-200">{label}</div>
                        {value && <div className="metric mt-0.5 text-ink-100">{value}</div>}
                      </button>
                    ),
                  )}
                </div>

                <Scales c={c} />

                <div className="mt-2 flex items-start gap-1.5 rounded-md bg-ink-900/70 px-2 py-1.5 text-[11px] leading-snug text-ink-300">
                  <Scale size={12} className="mt-0.5 shrink-0 text-signal" />
                  <span>{c.verdict}</span>
                </div>
              </div>
            )}
          </li>
        );
      })}
    </ul>
  );
}
