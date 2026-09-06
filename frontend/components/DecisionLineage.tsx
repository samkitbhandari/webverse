"use client";

import clsx from "clsx";
import { AlertTriangle, CheckCircle2, CornerDownRight } from "lucide-react";
import type { Lineage } from "@/lib/types";
import { EDGE_COLOR } from "@/lib/theme";
import { Empty, StatusChip, TypeChip } from "./ui";

/**
 * Why a decision was made, and whether that reasoning still holds.
 *
 * The "then vs now" columns are the point: a decision is not wrong because a
 * belief changed, but it does need revalidating, and that distinction is what
 * the panel is trying to make legible.
 */
export function DecisionLineage({
  lineage, onSelect,
}: {
  lineage: Lineage | null;
  onSelect?: (id: string) => void;
}) {
  if (!lineage) return <Empty>Select a decision to trace its evidential basis.</Empty>;

  const { decision, basis, still_valid, changed_since } = lineage;

  return (
    <div className="scroll-y h-full p-3">
      <div
        className={clsx(
          "rounded-md border px-3 py-2.5",
          still_valid ? "border-good/30 bg-good/5" : "border-danger/40 bg-danger/10",
        )}
      >
        <div className="flex items-start gap-2">
          {still_valid ? (
            <CheckCircle2 size={15} className="mt-0.5 shrink-0 text-good" />
          ) : (
            <AlertTriangle size={15} className="mt-0.5 shrink-0 text-danger" />
          )}
          <div className="min-w-0">
            <div className="flex items-center gap-1.5">
              <span className="metric text-[11px] text-ink-400">{decision.id}</span>
              <StatusChip status={decision.status} />
              <span className="text-[10px] text-ink-500">
                taken {decision.taken_at.slice(0, 10)}
              </span>
            </div>
            <p className="mt-1 text-xs leading-snug text-ink-100">{decision.label}</p>
            <p className={clsx("mt-1.5 text-[11px]", still_valid ? "text-good" : "text-danger")}>
              {still_valid
                ? "Every belief this decision rests on still stands."
                : `${changed_since.length} of ${basis.length} supporting belief(s) have moved. Revalidate.`}
            </p>
          </div>
        </div>
      </div>

      {basis.length === 0 ? (
        <div className="mt-3 rounded-md border border-warn/30 bg-warn/5 px-3 py-2 text-[11px] text-warn">
          No recorded evidential basis. This decision cannot be audited — attach the claims it
          rested on.
        </div>
      ) : (
        <ol className="mt-3 space-y-1.5">
          {basis.map((b) => (
            <li key={b.id}>
              <button
                onClick={() => onSelect?.(b.id)}
                className={clsx(
                  "w-full rounded-md border px-2.5 py-2 text-left transition hover:border-ink-500",
                  b.changed ? "border-danger/40 bg-danger/5" : "border-ink-700/60 bg-ink-850/60",
                )}
              >
                <div className="flex flex-wrap items-center gap-1.5">
                  <CornerDownRight size={11} className="shrink-0 text-ink-600" />
                  <span
                    className="shrink-0 rounded px-1 font-mono text-[9px] uppercase"
                    style={{
                      color: EDGE_COLOR[b.relationship] ?? "#8792a5",
                      backgroundColor: `${EDGE_COLOR[b.relationship] ?? "#8792a5"}1a`,
                    }}
                  >
                    {b.relationship}
                  </span>
                  <TypeChip type={b.type} />
                  <span className="metric text-[10px] text-ink-500">{b.id}</span>
                </div>

                <div className="mt-1 text-xs leading-snug text-ink-100">{b.label}</div>

                <div className="mt-1.5 flex items-center gap-2 text-[10px]">
                  <span className="text-ink-500">then</span>
                  <span className="text-ink-300">{b.status_then}</span>
                  <span className="text-ink-600">→</span>
                  <span className="text-ink-500">now</span>
                  <StatusChip status={b.status_now} />
                  {b.value_now && (
                    <span className="metric ml-auto text-ink-300">{b.value_now}</span>
                  )}
                </div>
              </button>
            </li>
          ))}
        </ol>
      )}
    </div>
  );
}
