"use client";

import clsx from "clsx";
import { AlertTriangle, ShieldCheck } from "lucide-react";
import type { KnowledgeHealth as Health } from "@/lib/types";
import { GRADE_COLOR, TYPE_COLOR } from "@/lib/theme";
import { Stat } from "./ui";

/** Circular gauge for the composite 0-100 knowledge-health score. */
function Gauge({ score, grade }: { score: number; grade: string }) {
  const color = GRADE_COLOR[grade] ?? "#8792a5";
  const r = 34;
  const circumference = 2 * Math.PI * r;
  const dash = (score / 100) * circumference;
  return (
    <div className="relative flex h-24 w-24 shrink-0 items-center justify-center">
      <svg viewBox="0 0 80 80" className="h-24 w-24 -rotate-90">
        <circle cx="40" cy="40" r={r} fill="none" stroke="#1f2632" strokeWidth="7" />
        <circle
          cx="40" cy="40" r={r} fill="none" stroke={color} strokeWidth="7"
          strokeLinecap="round"
          strokeDasharray={`${dash} ${circumference - dash}`}
          className="transition-all duration-700"
        />
      </svg>
      <div className="absolute flex flex-col items-center">
        <span className="metric text-xl leading-none" style={{ color }}>
          {score.toFixed(0)}
        </span>
        <span className="text-[9px] uppercase tracking-wider text-ink-400">{grade}</span>
      </div>
    </div>
  );
}

export function KnowledgeHealthPanel({ health }: { health: Health }) {
  const composition = Object.entries(health.by_type).sort((a, b) => b[1] - a[1]);
  const total = composition.reduce((sum, [, n]) => sum + n, 0) || 1;

  return (
    <div className="scroll-y h-full space-y-3 p-3">
      <div className="flex items-start gap-4">
        <Gauge score={health.score} grade={health.grade} />
        <div className="min-w-0 flex-1 space-y-1.5">
          {health.issues.map((issue, i) => (
            <div key={i} className="flex items-start gap-1.5 text-[11px] leading-snug text-ink-300">
              {health.grade === "healthy" && i === 0 ? (
                <ShieldCheck size={12} className="mt-0.5 shrink-0 text-good" />
              ) : (
                <AlertTriangle size={12} className="mt-0.5 shrink-0 text-warn" />
              )}
              <span>{issue}</span>
            </div>
          ))}
        </div>
      </div>

      <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
        <Stat
          label="Contradictions"
          value={health.open_contradictions}
          sub={`severity ${health.contradiction_severity.toFixed(2)}`}
          tone={health.open_contradictions ? "#ef476f" : undefined}
        />
        <Stat
          label="Decisions at risk"
          value={health.decisions_at_risk}
          sub={`${health.decisions_without_lineage} without lineage`}
          tone={health.decisions_at_risk ? "#f4a261" : undefined}
        />
        <Stat
          label="Avg confidence"
          value={health.average_confidence.toFixed(2)}
          sub={`uncertainty ${health.total_uncertainty.toFixed(1)}`}
        />
        <Stat
          label="Unsourced"
          value={health.unsourced_claims}
          sub={`${health.expired_beliefs} expired`}
          tone={health.unsourced_claims ? "#f4a261" : undefined}
        />
      </div>

      <div>
        <div className="mb-1.5 text-[10px] uppercase tracking-wider text-ink-400">
          Composition
        </div>
        <div className="flex h-2 overflow-hidden rounded-full bg-ink-800">
          {composition.map(([type, n]) => (
            <div
              key={type}
              title={`${type}: ${n}`}
              style={{ width: `${(n / total) * 100}%`, backgroundColor: TYPE_COLOR[type as never] ?? "#8792a5" }}
            />
          ))}
        </div>
        <div className="mt-1.5 flex flex-wrap gap-x-3 gap-y-1">
          {composition.map(([type, n]) => (
            <span key={type} className="flex items-center gap-1 text-[10px] text-ink-400">
              <span
                className="h-2 w-2 rounded-sm"
                style={{ backgroundColor: TYPE_COLOR[type as never] ?? "#8792a5" }}
              />
              {type} <span className="metric text-ink-300">{n}</span>
            </span>
          ))}
        </div>
      </div>

      <div>
        <div className="mb-1 text-[10px] uppercase tracking-wider text-ink-400">
          Most depended upon
        </div>
        <ul className="space-y-1">
          {health.critical_nodes.slice(0, 5).map((n) => (
            <li key={n.id} className="flex items-center gap-2 text-[11px]">
              <span className="metric w-9 shrink-0 text-ink-400">{n.criticality.toFixed(2)}</span>
              <span
                className="h-1.5 w-1.5 shrink-0 rounded-full"
                style={{ backgroundColor: TYPE_COLOR[n.type as never] ?? "#8792a5" }}
              />
              <span className="truncate text-ink-200">{n.label}</span>
              <span className="metric ml-auto shrink-0 text-ink-500">{n.dependents}↓</span>
            </li>
          ))}
        </ul>
      </div>
    </div>
  );
}
