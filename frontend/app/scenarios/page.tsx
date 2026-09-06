"use client";

import clsx from "clsx";
import { useCallback, useEffect, useMemo, useState } from "react";
import { Check, Play, Plus, Split, Trash2, Trophy } from "lucide-react";
import { useStream } from "@/components/Shell";
import { Empty, ErrorNote, Panel, PathChain, Spinner, TypeChip } from "@/components/ui";
import { api } from "@/lib/api";
import { useLiveData } from "@/lib/websocket";
import { formatNumber } from "@/lib/theme";
import type { NodeDetail, ScenarioComparison } from "@/lib/types";

type InterventionType =
  | "SET_VALUE" | "INVALIDATE" | "ASSERT" | "ADD_DEPENDENCY" | "REMOVE_DEPENDENCY";

interface Draft {
  id: string;
  type: InterventionType;
  target?: string;
  source?: string;
  subject?: string;
  predicate?: string;
  value?: string;
  unit?: string;
  edge_type?: string;
  note?: string;
}

interface ScenarioDraft {
  id: string;
  name: string;
  description: string;
  interventions: Draft[];
}

const NEEDS_TARGET: InterventionType[] = ["SET_VALUE", "INVALIDATE", "ADD_DEPENDENCY", "REMOVE_DEPENDENCY"];
const NEEDS_SOURCE: InterventionType[] = ["ADD_DEPENDENCY", "REMOVE_DEPENDENCY"];
const NEEDS_VALUE: InterventionType[] = ["SET_VALUE", "ASSERT"];

const uid = () => Math.random().toString(36).slice(2, 9);

const blankScenario = (n: number): ScenarioDraft => ({
  id: uid(),
  name: `Scenario ${String.fromCharCode(64 + n)}`,
  description: "",
  interventions: [{ id: uid(), type: "INVALIDATE" }],
});

/** Metrics worth showing as a delta row; the rest are noise in a comparison. */
const HEADLINE = [
  "health_score", "open_contradictions", "compliance_violations",
  "decisions_at_risk", "risk_exposure", "cost", "schedule", "capacity",
  "total_uncertainty",
];

function DeltaRow({ d }: { d: { name: string; before: number | null; after: number | null; delta: number | null; direction: string } }) {
  const tone =
    d.direction === "better" ? "text-good" : d.direction === "worse" ? "text-danger" : "text-ink-400";
  return (
    <div className="flex items-center gap-2 py-0.5 text-[11px]">
      <span className="w-36 shrink-0 text-ink-400">{d.name.replace(/_/g, " ")}</span>
      <span className="metric w-20 shrink-0 text-right text-ink-500">{formatNumber(d.before)}</span>
      <span className="shrink-0 text-ink-600">→</span>
      <span className={clsx("metric w-20 shrink-0", tone)}>{formatNumber(d.after)}</span>
      <span className={clsx("metric ml-auto shrink-0", tone)}>
        {d.delta === null ? "" : `${d.delta > 0 ? "+" : ""}${formatNumber(d.delta)}`}
      </span>
    </div>
  );
}

export default function ScenariosPage() {
  const { lastChange } = useStream();
  const [scenarios, setScenarios] = useState<ScenarioDraft[]>([blankScenario(1), blankScenario(2)]);
  const [result, setResult] = useState<ScenarioComparison | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const nodes = useLiveData(() => api.nodes({ limit: 400 }), [lastChange]);
  const options = useMemo(() => nodes.data?.items ?? [], [nodes.data]);

  const patch = useCallback((sid: string, iid: string, fields: Partial<Draft>) => {
    setScenarios((prev) =>
      prev.map((s) =>
        s.id !== sid
          ? s
          : { ...s, interventions: s.interventions.map((i) => (i.id === iid ? { ...i, ...fields } : i)) },
      ),
    );
  }, []);

  const run = useCallback(async () => {
    setBusy(true);
    setError(null);
    try {
      const payload = scenarios.map((s) => ({
        name: s.name,
        description: s.description,
        interventions: s.interventions.map(({ id, ...rest }) => ({
          ...rest,
          edge_type: rest.edge_type ?? "DEPENDS_ON",
        })),
      }));
      setResult(await api.compareScenarios(payload));
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      setResult(null);
    } finally {
      setBusy(false);
    }
  }, [scenarios]);

  /** Seed a plausible pair of scenarios so the page is never empty on arrival. */
  useEffect(() => {
    if (options.length === 0 || result) return;
    const req = options.find((n) => n.type === "Requirement");
    const entity = options.find((n) => n.type === "Entity" && /architecture|thermal/i.test(n.label));
    const other = options.find((n) => n.type === "Entity" && n.id !== entity?.id);
    setScenarios((prev) => {
      if (prev.some((s) => s.interventions.some((i) => i.target || i.subject))) return prev;
      return [
        {
          id: uid(),
          name: "A: relax the constraint",
          description: "Assume the tighter limit can be met by redesign.",
          interventions: req
            ? [{ id: uid(), type: "SET_VALUE", target: req.id, value: "55", unit: "degC" }]
            : [{ id: uid(), type: "INVALIDATE", target: options[0]?.id }],
        },
        {
          id: uid(),
          name: "B: withdraw the design",
          description: "Assume the current architecture is abandoned.",
          interventions: entity
            ? [{ id: uid(), type: "INVALIDATE", target: entity.id }]
            : [{ id: uid(), type: "INVALIDATE", target: other?.id ?? options[0]?.id }],
        },
      ];
    });
  }, [options, result]);

  return (
    <div className="grid h-full grid-cols-12 gap-2 p-2">
      <Panel
        className="col-span-12 lg:col-span-5"
        title={
          <span className="flex items-center gap-1.5">
            <Split size={12} /> Interventions
          </span>
        }
        actions={
          <div className="flex gap-1">
            <button
              className="btn"
              onClick={() => setScenarios((p) => [...p, blankScenario(p.length + 1)])}
              disabled={scenarios.length >= 4}
            >
              <Plus size={12} /> scenario
            </button>
            <button className="btn btn-primary" onClick={run} disabled={busy || scenarios.length < 2}>
              <Play size={12} /> simulate
            </button>
          </div>
        }
      >
        <div className="scroll-y h-full space-y-2 p-2">
          {scenarios.map((s) => (
            <div key={s.id} className="rounded-md border border-ink-700/60 bg-ink-850/50 p-2">
              <div className="flex items-center gap-1.5">
                <input
                  value={s.name}
                  onChange={(e) =>
                    setScenarios((p) => p.map((x) => (x.id === s.id ? { ...x, name: e.target.value } : x)))
                  }
                  className="min-w-0 flex-1 rounded border border-ink-700 bg-ink-900 px-2 py-1 text-xs text-ink-100 outline-none focus:border-signal/60"
                />
                <button
                  className="rounded p-1 text-ink-500 hover:bg-ink-800 hover:text-danger"
                  onClick={() => setScenarios((p) => p.filter((x) => x.id !== s.id))}
                  disabled={scenarios.length <= 2}
                >
                  <Trash2 size={13} />
                </button>
              </div>
              <input
                value={s.description}
                onChange={(e) =>
                  setScenarios((p) => p.map((x) => (x.id === s.id ? { ...x, description: e.target.value } : x)))
                }
                placeholder="what this scenario assumes"
                className="mt-1 w-full rounded border border-ink-700 bg-ink-900 px-2 py-1 text-[11px] text-ink-300 outline-none placeholder:text-ink-600 focus:border-signal/60"
              />

              <div className="mt-2 space-y-1.5">
                {s.interventions.map((iv) => (
                  <div key={iv.id} className="rounded border border-ink-700/60 bg-ink-900/60 p-1.5">
                    <div className="flex gap-1.5">
                      <select
                        value={iv.type}
                        onChange={(e) => patch(s.id, iv.id, { type: e.target.value as InterventionType })}
                        className="rounded border border-ink-700 bg-ink-850 px-1.5 py-1 text-[11px] text-ink-100 outline-none"
                      >
                        {(["SET_VALUE", "INVALIDATE", "ASSERT", "ADD_DEPENDENCY", "REMOVE_DEPENDENCY"] as const).map(
                          (t) => (
                            <option key={t} value={t}>
                              {t.replace(/_/g, " ").toLowerCase()}
                            </option>
                          ),
                        )}
                      </select>
                      <button
                        className="ml-auto rounded p-1 text-ink-500 hover:text-danger"
                        onClick={() =>
                          setScenarios((p) =>
                            p.map((x) =>
                              x.id === s.id
                                ? { ...x, interventions: x.interventions.filter((i) => i.id !== iv.id) }
                                : x,
                            ),
                          )
                        }
                        disabled={s.interventions.length <= 1}
                      >
                        <Trash2 size={12} />
                      </button>
                    </div>

                    <div className="mt-1.5 grid grid-cols-2 gap-1.5">
                      {NEEDS_SOURCE.includes(iv.type) && (
                        <select
                          value={iv.source ?? ""}
                          onChange={(e) => patch(s.id, iv.id, { source: e.target.value })}
                          className="col-span-2 rounded border border-ink-700 bg-ink-850 px-1.5 py-1 text-[11px] text-ink-100 outline-none"
                        >
                          <option value="">from node…</option>
                          {options.map((n) => (
                            <option key={n.id} value={n.id}>
                              {n.id} · {n.label.slice(0, 50)}
                            </option>
                          ))}
                        </select>
                      )}
                      {NEEDS_TARGET.includes(iv.type) && (
                        <select
                          value={iv.target ?? ""}
                          onChange={(e) => patch(s.id, iv.id, { target: e.target.value })}
                          className="col-span-2 rounded border border-ink-700 bg-ink-850 px-1.5 py-1 text-[11px] text-ink-100 outline-none"
                        >
                          <option value="">target node…</option>
                          {options.map((n) => (
                            <option key={n.id} value={n.id}>
                              {n.id} · {n.label.slice(0, 50)}
                            </option>
                          ))}
                        </select>
                      )}
                      {iv.type === "ASSERT" && (
                        <>
                          <input
                            value={iv.subject ?? ""}
                            onChange={(e) => patch(s.id, iv.id, { subject: e.target.value })}
                            placeholder="subject"
                            className="rounded border border-ink-700 bg-ink-850 px-1.5 py-1 text-[11px] outline-none"
                          />
                          <input
                            value={iv.predicate ?? ""}
                            onChange={(e) => patch(s.id, iv.id, { predicate: e.target.value })}
                            placeholder="predicate"
                            className="rounded border border-ink-700 bg-ink-850 px-1.5 py-1 text-[11px] outline-none"
                          />
                        </>
                      )}
                      {NEEDS_VALUE.includes(iv.type) && (
                        <>
                          <input
                            value={iv.value ?? ""}
                            onChange={(e) => patch(s.id, iv.id, { value: e.target.value })}
                            placeholder="value"
                            className="rounded border border-ink-700 bg-ink-850 px-1.5 py-1 text-[11px] outline-none"
                          />
                          <input
                            value={iv.unit ?? ""}
                            onChange={(e) => patch(s.id, iv.id, { unit: e.target.value })}
                            placeholder="unit"
                            className="rounded border border-ink-700 bg-ink-850 px-1.5 py-1 text-[11px] outline-none"
                          />
                        </>
                      )}
                    </div>
                  </div>
                ))}
                <button
                  className="btn w-full justify-center"
                  onClick={() =>
                    setScenarios((p) =>
                      p.map((x) =>
                        x.id === s.id
                          ? { ...x, interventions: [...x.interventions, { id: uid(), type: "INVALIDATE" }] }
                          : x,
                      ),
                    )
                  }
                >
                  <Plus size={11} /> intervention
                </button>
              </div>
            </div>
          ))}
        </div>
      </Panel>

      <Panel
        className="col-span-12 lg:col-span-7"
        title="Counterfactual comparison"
        actions={
          result?.comparison.recommended && (
            <span className="flex items-center gap-1 text-[11px] text-good">
              <Trophy size={12} /> {result.comparison.recommended}
            </span>
          )
        }
      >
        {error && <ErrorNote message={error} />}
        {busy && <Spinner label="cloning the world and re-reasoning" />}
        {!busy && !result && (
          <Empty>
            Define two or more scenarios and simulate. Each runs against a detached clone of the
            graph, so nothing here can alter the real knowledge state.
          </Empty>
        )}

        {!busy && result && (
          <div className="scroll-y h-full space-y-2 p-2">
            <div className="rounded-md border border-good/30 bg-good/5 px-3 py-2 text-[11px] leading-snug text-ink-200">
              {result.comparison.rationale}
            </div>

            {result.comparison.ranking.map((row, i) => {
              const detail = result.scenarios.find((s) => s.scenario === row.scenario);
              const deltas = (detail?.deltas ?? []).filter(
                (d) => HEADLINE.includes(d.name) && (d.before !== null || d.after !== null),
              );
              return (
                <div
                  key={row.scenario}
                  className={clsx(
                    "rounded-md border p-2.5",
                    i === 0 ? "border-good/40 bg-good/5" : "border-ink-700/60 bg-ink-850/50",
                  )}
                >
                  <div className="flex items-center gap-2">
                    {i === 0 && <Check size={13} className="shrink-0 text-good" />}
                    <span className="text-xs font-medium text-ink-100">{row.scenario}</span>
                    <span className="metric ml-auto text-[11px] text-ink-400">
                      score {row.score.toFixed(2)}
                    </span>
                  </div>

                  {detail?.description && (
                    <p className="mt-0.5 text-[11px] text-ink-400">{detail.description}</p>
                  )}

                  <ul className="mt-1.5 space-y-0.5">
                    {detail?.interventions.map((t, k) => (
                      <li key={k} className="text-[10px] text-ink-500">
                        · {t}
                      </li>
                    ))}
                  </ul>

                  <div className="mt-2 border-t border-ink-800/70 pt-1.5">{deltas.map((d) => (
                    <DeltaRow key={d.name} d={d} />
                  ))}</div>

                  {(detail?.newly_at_risk.length || detail?.resolved_decisions.length) ? (
                    <div className="mt-1.5 flex flex-wrap gap-1.5 text-[10px]">
                      {detail?.resolved_decisions.map((d) => (
                        <span key={d.id} className="chip bg-good/15 text-good">
                          resolves {d.id}
                        </span>
                      ))}
                      {detail?.newly_at_risk.map((d) => (
                        <span key={d.id} className="chip bg-danger/15 text-danger">
                          risks {d.id}
                        </span>
                      ))}
                    </div>
                  ) : null}

                  {detail?.first_order.length ? (
                    <details className="mt-1.5">
                      <summary className="cursor-pointer text-[10px] uppercase tracking-wider text-ink-500">
                        {detail.first_order.length} first-order · {detail.second_order.length} second-order
                      </summary>
                      <ul className="mt-1 space-y-1">
                        {[...detail.first_order, ...detail.second_order].slice(0, 8).map((e) => (
                          <li key={e.id} className="flex items-start gap-1.5 text-[10px]">
                            <span className="metric w-9 shrink-0 text-ink-500">
                              {e.severity.toFixed(3)}
                            </span>
                            <TypeChip type={e.type} />
                            <span className="min-w-0 flex-1">
                              <PathChain text={e.explanation} />
                            </span>
                          </li>
                        ))}
                      </ul>
                    </details>
                  ) : null}

                  <p className="mt-1.5 text-[11px] leading-snug text-ink-300">{row.verdict}</p>
                </div>
              );
            })}
          </div>
        )}
      </Panel>
    </div>
  );
}
