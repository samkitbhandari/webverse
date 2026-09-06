"use client";

import clsx from "clsx";
import { useState } from "react";
import { ExternalLink, Quote, Radar, X } from "lucide-react";
import type { NodeDetail } from "@/lib/types";
import { EDGE_COLOR, timeAgo } from "@/lib/theme";
import { Empty, Meter, Spinner, StatusChip, TypeChip } from "./ui";

const TABS = ["overview", "evidence", "links"] as const;
type Tab = (typeof TABS)[number];

export function NodeInspector({
  node, loading, onClose, onSelect, onImpact,
}: {
  node: NodeDetail | null;
  loading?: boolean;
  onClose?: () => void;
  onSelect?: (id: string) => void;
  onImpact?: (id: string) => void;
}) {
  const [tab, setTab] = useState<Tab>("overview");

  if (loading) return <Spinner label="loading node" />;
  if (!node) return <Empty>Select a node to inspect its evidence, links and history.</Empty>;

  const out = node.relationships?.outgoing ?? [];
  const inc = node.relationships?.incoming ?? [];

  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="shrink-0 border-b border-ink-800/70 px-3 py-2.5">
        <div className="flex items-start justify-between gap-2">
          <div className="min-w-0">
            <div className="flex flex-wrap items-center gap-1.5">
              <span className="metric text-[11px] text-ink-400">{node.id}</span>
              <TypeChip type={node.type} />
              <StatusChip status={node.status} />
            </div>
            <h3 className="mt-1 text-sm leading-snug text-ink-100">{node.label}</h3>
          </div>
          {onClose && (
            <button onClick={onClose} className="shrink-0 rounded p-1 text-ink-400 hover:bg-ink-800 hover:text-ink-100">
              <X size={14} />
            </button>
          )}
        </div>

        <div className="mt-2 flex gap-1">
          {TABS.map((t) => (
            <button
              key={t}
              onClick={() => setTab(t)}
              className={clsx(
                "rounded px-2 py-1 text-[11px] capitalize transition",
                tab === t ? "bg-ink-700 text-ink-100" : "text-ink-400 hover:bg-ink-800",
              )}
            >
              {t}
              {t === "links" && ` (${out.length + inc.length})`}
              {t === "evidence" && ` (${node.provenance.length})`}
            </button>
          ))}
          {onImpact && (
            <button className="btn btn-primary ml-auto" onClick={() => onImpact(node.id)}>
              <Radar size={12} /> Impact
            </button>
          )}
        </div>
      </div>

      <div className="scroll-y min-h-0 flex-1 p-3">
        {tab === "overview" && (
          <div className="space-y-3">
            {node.body && (
              <p className="whitespace-pre-wrap text-xs leading-relaxed text-ink-300">{node.body}</p>
            )}

            {node.subject && node.predicate && (
              <div className="rounded-md border border-ink-700/60 bg-ink-850/60 px-2.5 py-2">
                <div className="text-[10px] uppercase tracking-wider text-ink-400">Assertion</div>
                <div className="mt-1 flex flex-wrap items-baseline gap-1.5 text-xs">
                  <span className="text-ink-200">{node.subject}</span>
                  <span className="text-ink-600">/</span>
                  <span className="text-ink-300">{node.predicate}</span>
                  <span className="metric text-ink-500">
                    {node.operator
                      ? { LT: "<", LTE: "≤", GT: ">", GTE: "≥", EQ: "=", NEQ: "≠" }[node.operator] ?? node.operator
                      : "="}
                  </span>
                  <span className="metric text-sm text-signal">{node.value ?? "—"}</span>
                </div>
              </div>
            )}

            <div className="space-y-1.5">
              <Meter label="confidence" value={node.confidence} tone="#4cc9f0" />
              <Meter label="criticality" value={node.criticality} tone="#f4a261" />
            </div>

            {node.credibility && (
              <div>
                <div className="mb-1 text-[10px] uppercase tracking-wider text-ink-400">
                  Credibility ({node.credibility.total.toFixed(3)})
                </div>
                <div className="space-y-1">
                  {(["authority", "recency", "corroboration", "specificity", "temporal_validity"] as const).map(
                    (k) => (
                      <Meter key={k} label={k.replace("_", " ")} value={node.credibility![k]} tone="#7aa2f7" />
                    ),
                  )}
                </div>
              </div>
            )}

            <dl className="grid grid-cols-2 gap-2 text-[11px]">
              <div>
                <dt className="text-ink-500">valid from</dt>
                <dd className="metric text-ink-200">{node.valid_from?.slice(0, 10) ?? "—"}</dd>
              </div>
              <div>
                <dt className="text-ink-500">valid until</dt>
                <dd className="metric text-ink-200">{node.valid_until?.slice(0, 10) ?? "present"}</dd>
              </div>
              <div>
                <dt className="text-ink-500">first seen</dt>
                <dd className="metric text-ink-200">{timeAgo(node.created_at)}</dd>
              </div>
              <div>
                <dt className="text-ink-500">unit</dt>
                <dd className="metric text-ink-200">{node.unit ?? "—"}</dd>
              </div>
            </dl>
          </div>
        )}

        {tab === "evidence" && (
          <div className="space-y-2">
            {node.provenance.length === 0 && (
              <Empty>No recorded provenance — this belief is unsourced.</Empty>
            )}
            {node.provenance.map((p, i) => (
              <div key={i} className="rounded-md border border-ink-700/60 bg-ink-850/60 px-2.5 py-2">
                <div className="flex items-center justify-between gap-2">
                  <span className="truncate text-xs text-ink-100">{p.source ?? "unknown source"}</span>
                  <span className="metric shrink-0 text-[10px] text-ink-400">
                    authority {p.authority.toFixed(2)}
                  </span>
                </div>
                {p.locator && <div className="mt-0.5 text-[10px] text-ink-500">{p.locator}</div>}
                {p.excerpt && (
                  <div className="mt-1.5 flex gap-1.5 rounded bg-ink-900/70 px-2 py-1.5">
                    <Quote size={11} className="mt-0.5 shrink-0 text-ink-600" />
                    <p className="text-[11px] leading-snug text-ink-300">{p.excerpt}</p>
                  </div>
                )}
                <div className="mt-1 flex items-center gap-2 text-[10px] text-ink-500">
                  <span>extracted by {p.extractor}</span>
                  {p.observed_at && <span>· {p.observed_at.slice(0, 10)}</span>}
                </div>
              </div>
            ))}
          </div>
        )}

        {tab === "links" && (
          <div className="space-y-3">
            {[["outgoing", out] as const, ["incoming", inc] as const].map(([label, rels]) =>
              rels.length === 0 ? null : (
                <div key={label}>
                  <div className="mb-1 text-[10px] uppercase tracking-wider text-ink-400">{label}</div>
                  <ul className="space-y-1">
                    {rels.map((r, i) => {
                      const otherId = r.target ?? r.source ?? "";
                      const otherLabel = r.target_label ?? r.source_label ?? otherId;
                      return (
                        <li key={`${label}-${i}`}>
                          <button
                            onClick={() => onSelect?.(otherId)}
                            className="row-hover flex w-full items-center gap-2 rounded px-1.5 py-1 text-left text-[11px]"
                          >
                            <span
                              className="shrink-0 rounded px-1 font-mono text-[9px] uppercase"
                              style={{
                                color: EDGE_COLOR[r.type] ?? "#8792a5",
                                backgroundColor: `${EDGE_COLOR[r.type] ?? "#8792a5"}1a`,
                              }}
                            >
                              {r.type}
                            </span>
                            <span className="truncate text-ink-200">{otherLabel}</span>
                            <span className="metric ml-auto shrink-0 text-[10px] text-ink-500">
                              w{r.weight.toFixed(2)}
                            </span>
                            <ExternalLink size={10} className="shrink-0 text-ink-600" />
                          </button>
                          {r.rationale && (
                            <p className="ml-2 pl-1.5 text-[10px] leading-snug text-ink-500">{r.rationale}</p>
                          )}
                        </li>
                      );
                    })}
                  </ul>
                </div>
              ),
            )}
            {out.length + inc.length === 0 && <Empty>This node has no relationships.</Empty>}
          </div>
        )}
      </div>
    </div>
  );
}
