"use client";

import clsx from "clsx";
import type { ReactNode } from "react";
import type { EpistemicStatus, NodeType } from "@/lib/types";
import { STATUS_COLOR, STATUS_LABEL, TYPE_COLOR } from "@/lib/theme";

export function Panel({
  title, actions, children, className, bodyClassName,
}: {
  title?: ReactNode;
  actions?: ReactNode;
  children: ReactNode;
  className?: string;
  bodyClassName?: string;
}) {
  return (
    <section className={clsx("panel flex min-h-0 flex-col", className)}>
      {(title || actions) && (
        <header className="panel-header shrink-0">
          <h2 className="panel-title">{title}</h2>
          {actions}
        </header>
      )}
      <div className={clsx("min-h-0 flex-1", bodyClassName)}>{children}</div>
    </section>
  );
}

export function TypeChip({ type, className }: { type: NodeType | string; className?: string }) {
  const color = TYPE_COLOR[type as NodeType] ?? "#8792a5";
  return (
    <span
      className={clsx("chip", className)}
      style={{ color, backgroundColor: `${color}1f`, border: `1px solid ${color}33` }}
    >
      {type}
    </span>
  );
}

export function StatusChip({ status }: { status: EpistemicStatus | string }) {
  const color = STATUS_COLOR[status as EpistemicStatus] ?? "#8792a5";
  return (
    <span
      className="chip"
      style={{ color, backgroundColor: `${color}1a`, border: `1px solid ${color}30` }}
    >
      {STATUS_LABEL[status as EpistemicStatus] ?? String(status).toLowerCase()}
    </span>
  );
}

export function Meter({
  value, label, tone = "#4cc9f0", max = 1,
}: {
  value: number;
  label?: string;
  tone?: string;
  max?: number;
}) {
  const pct = Math.max(0, Math.min(100, (value / max) * 100));
  return (
    <div className="flex items-center gap-2">
      {label && <span className="w-28 shrink-0 text-[11px] text-ink-300">{label}</span>}
      <div className="h-1.5 flex-1 overflow-hidden rounded-full bg-ink-800">
        <div
          className="h-full rounded-full transition-all duration-500"
          style={{ width: `${pct}%`, backgroundColor: tone }}
        />
      </div>
      <span className="metric w-10 shrink-0 text-right text-[11px] text-ink-300">
        {value.toFixed(2)}
      </span>
    </div>
  );
}

export function Stat({
  label, value, sub, tone,
}: {
  label: string;
  value: ReactNode;
  sub?: ReactNode;
  tone?: string;
}) {
  return (
    <div className="rounded-md border border-ink-700/60 bg-ink-850/60 px-3 py-2">
      <div className="text-[10px] uppercase tracking-wider text-ink-400">{label}</div>
      <div className="metric mt-0.5 text-lg leading-tight" style={tone ? { color: tone } : undefined}>
        {value}
      </div>
      {sub && <div className="mt-0.5 text-[11px] text-ink-400">{sub}</div>}
    </div>
  );
}

export function Empty({ children }: { children: ReactNode }) {
  return (
    <div className="flex h-full min-h-[80px] items-center justify-center px-4 py-6 text-center text-xs text-ink-400">
      {children}
    </div>
  );
}

export function Spinner({ label }: { label?: string }) {
  return (
    <div className="flex h-full min-h-[80px] items-center justify-center gap-2 text-xs text-ink-400">
      <span className="h-3 w-3 animate-spin rounded-full border border-ink-500 border-t-signal" />
      {label ?? "loading"}
    </div>
  );
}

export function ErrorNote({ message }: { message: string }) {
  return (
    <div className="m-3 rounded-md border border-danger/40 bg-danger/10 px-3 py-2 text-xs text-danger">
      {message}
    </div>
  );
}

/** Renders "A --EDGE--> B --EDGE--> C" as a readable, wrapping chain. */
export function PathChain({ text }: { text: string }) {
  const parts = text.split(/\s(?=--)|(?<=-->)\s/);
  return (
    <span className="inline-flex flex-wrap items-center gap-x-1.5 gap-y-1 leading-relaxed">
      {parts.map((part, i) =>
        part.startsWith("--") ? (
          <span
            key={i}
            className="rounded bg-ink-800 px-1 py-px font-mono text-[9px] uppercase tracking-wide text-ink-400"
          >
            {part.replace(/^--|-->$/g, "")}
          </span>
        ) : (
          <span key={i} className="text-ink-200">
            {part}
          </span>
        ),
      )}
    </span>
  );
}
