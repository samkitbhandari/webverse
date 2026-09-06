"use client";

import clsx from "clsx";
import { useMemo } from "react";
import {
  AlertTriangle, ArrowRightLeft, FileInput, GitCommitHorizontal,
  Radar, Sparkles, Zap,
} from "lucide-react";
import type { NexusEvent } from "@/lib/types";
import { EVENT_LABEL, EVENT_TONE, timeAgo } from "@/lib/theme";
import { Empty } from "./ui";

const TONE_CLASS = {
  info: "text-ink-300",
  good: "text-good",
  warn: "text-warn",
  danger: "text-danger",
} as const;

function iconFor(type: string) {
  if (type.startsWith("SOURCE_")) return FileInput;
  if (type === "CONTRADICTION_DETECTED" || type === "CONSTRAINT_VIOLATED") return AlertTriangle;
  if (type === "IMPACT_COMPUTED" || type === "DECISION_AFFECTED") return Radar;
  if (type === "KNOWLEDGE_DRIFT" || type === "CLAIM_CHANGED") return ArrowRightLeft;
  if (type === "GRAPH_CHANGED") return GitCommitHorizontal;
  if (type === "PIPELINE_STAGE") return Zap;
  return Sparkles;
}

/** One line of detail per event type — the payload is not shown raw. */
function describe(e: NexusEvent): string {
  const p = e.payload ?? {};
  switch (e.event_type) {
    case "CONNECTED":
      return `${p.stats?.nodes ?? 0} nodes, ${p.stats?.edges ?? 0} relationships`;
    case "PIPELINE_STAGE":
      return p.stage === "error"
        ? `${p.source}: ${p.message ?? "failed"}`
        : `${p.stage}${p.source ? ` · ${p.source}` : ""}${p.chunks ? ` · ${p.chunks}/${p.of} chunks` : ""}`;
    case "GRAPH_CHANGED":
      if (p.reason === "source_removed") return `${p.path?.split(/[\\/]/).pop()} removed · ${p.affected} weakened`;
      return `${p.source} · +${p.nodes_added} nodes, +${p.edges_added} edges${
        p.contradictions ? `, ${p.contradictions} contradiction(s)` : ""
      } · ${p.duration_ms}ms`;
    case "ENTITY_ADDED":
      return `${p.label} (${p.kind ?? "entity"})`;
    case "CLAIM_ADDED":
      return p.description ?? p.label ?? "";
    case "CLAIM_CHANGED":
    case "KNOWLEDGE_DRIFT":
      return p.description ?? "";
    case "CLAIM_SUPERSEDED":
      return `${p.new_label ?? p.new} replaces ${p.old_label ?? p.old}`;
    case "CONTRADICTION_DETECTED":
    case "CONSTRAINT_VIOLATED":
      return p.description ?? "";
    case "IMPACT_COMPUTED":
      return `${p.affected} node(s) affected from ${(p.seed_labels ?? []).join(", ") || p.seeds?.join(", ")}`;
    case "DECISION_AFFECTED":
      return `${p.label} · severity ${Number(p.severity ?? 0).toFixed(2)}`;
    case "RELATIONSHIP_ADDED":
      return `${p.source} --${p.type}--> ${p.target}`;
    default:
      return "";
  }
}

export function EventFeed({ events, limit = 60 }: { events: NexusEvent[]; limit?: number }) {
  const items = useMemo(() => events.slice(0, limit), [events, limit]);

  if (items.length === 0) {
    return <Empty>No activity yet. Drop a document into incoming_files/ to begin.</Empty>;
  }

  return (
    <ul className="scroll-y h-full divide-y divide-ink-800/70">
      {items.map((e, i) => {
        const tone = EVENT_TONE[e.event_type] ?? "info";
        const Icon = iconFor(e.event_type);
        const detail = describe(e);
        return (
          <li
            key={`${e.event_id ?? "x"}-${i}`}
            className={clsx(
              "flex gap-2.5 px-3 py-2 text-xs animate-slideIn",
              e.replayed && "opacity-60",
            )}
          >
            <Icon size={13} className={clsx("mt-0.5 shrink-0", TONE_CLASS[tone])} strokeWidth={2} />
            <div className="min-w-0 flex-1">
              <div className="flex items-baseline justify-between gap-2">
                <span className={clsx("font-medium", TONE_CLASS[tone])}>
                  {EVENT_LABEL[e.event_type] ?? e.event_type.toLowerCase().replace(/_/g, " ")}
                </span>
                <span className="shrink-0 text-[10px] text-ink-500">{timeAgo(e.timestamp)}</span>
              </div>
              {detail && <p className="mt-0.5 break-words text-ink-300">{detail}</p>}
            </div>
          </li>
        );
      })}
    </ul>
  );
}
