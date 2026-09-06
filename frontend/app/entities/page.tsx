"use client";

import { useEffect, useMemo, useState } from "react";
import { FileUp, Search, Sparkles } from "lucide-react";
import { NodeInspector } from "@/components/NodeInspector";
import { Timeline } from "@/components/Timeline";
import { useStream } from "@/components/Shell";
import { Empty, ErrorNote, Panel, Spinner, StatusChip, TypeChip } from "@/components/ui";
import { api } from "@/lib/api";
import { useLiveData } from "@/lib/websocket";
import type { NodeDetail, NodeType, Timeline as TL } from "@/lib/types";

const GROUPS: { label: string; types: string }[] = [
  { label: "All", types: "" },
  { label: "Entities", types: "Entity" },
  { label: "Claims", types: "Claim,Observation,Assumption" },
  { label: "Rules", types: "Requirement,Constraint" },
  { label: "Events", types: "Event" },
  { label: "Risks", types: "Risk,Action" },
  { label: "Sources", types: "Evidence" },
];

export default function KnowledgePage() {
  const { lastChange } = useStream();
  const [group, setGroup] = useState(0);
  const [query, setQuery] = useState("");
  const [semantic, setSemantic] = useState(false);
  const [selected, setSelected] = useState<string | null>(null);
  const [timeline, setTimeline] = useState<TL | null>(null);
  const [draft, setDraft] = useState("");
  const [ingestNote, setIngestNote] = useState<string | null>(null);

  const list = useLiveData(
    () =>
      semantic && query.length > 1
        ? api.search(query, 25).then((r) => ({
            total: r.hits.length,
            offset: 0,
            items: r.hits
              .map((h) => h.node)
              .filter(Boolean) as NodeDetail[],
          }))
        : api.nodes({
            type: GROUPS[group].types || undefined,
            q: query || undefined,
            limit: 200,
          }),
    [lastChange, group, query, semantic],
  );

  const detail = useLiveData<NodeDetail | null>(
    () => (selected ? api.node(selected) : Promise.resolve(null)),
    [selected, lastChange],
  );

  useEffect(() => {
    if (!selected) {
      setTimeline(null);
      return;
    }
    api.timeline(selected).then(setTimeline).catch(() => setTimeline(null));
  }, [selected, lastChange]);

  const grouped = useMemo(() => {
    const map = new Map<NodeType, NodeDetail[]>();
    for (const n of list.data?.items ?? []) {
      const arr = map.get(n.type) ?? [];
      arr.push(n);
      map.set(n.type, arr);
    }
    return [...map.entries()].sort((a, b) => b[1].length - a[1].length);
  }, [list.data]);

  const submit = async () => {
    if (!draft.trim()) return;
    setIngestNote(null);
    try {
      const res = await api.ingestText(draft.trim());
      setIngestNote(`queued ${res.path.split(/[\\/]/).pop()} — watch the activity feed`);
      setDraft("");
    } catch (e) {
      setIngestNote(e instanceof Error ? e.message : String(e));
    }
  };

  return (
    <div className="grid h-full grid-cols-12 gap-2 p-2">
      <Panel
        className="col-span-12 lg:col-span-5"
        title="Knowledge base"
        actions={<span className="metric text-[10px] text-ink-500">{list.data?.total ?? 0}</span>}
      >
        <div className="flex h-full min-h-0 flex-col">
          <div className="shrink-0 space-y-2 border-b border-ink-800/70 p-2">
            <div className="flex items-center gap-1.5 rounded-md border border-ink-700 bg-ink-900 px-2 py-1.5">
              <Search size={12} className="shrink-0 text-ink-500" />
              <input
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                placeholder={semantic ? "semantic search over sources and nodes" : "filter by text"}
                className="w-full bg-transparent text-xs text-ink-100 outline-none placeholder:text-ink-600"
              />
              <button
                onClick={() => setSemantic((v) => !v)}
                title="Toggle embedding-based retrieval"
                className={`shrink-0 rounded px-1 py-0.5 ${
                  semantic ? "bg-signal/20 text-signal" : "text-ink-500 hover:text-ink-300"
                }`}
              >
                <Sparkles size={12} />
              </button>
            </div>
            <div className="flex flex-wrap gap-1">
              {GROUPS.map((g, i) => (
                <button
                  key={g.label}
                  onClick={() => {
                    setGroup(i);
                    setSemantic(false);
                  }}
                  className={`rounded px-2 py-1 text-[11px] transition ${
                    group === i && !semantic
                      ? "bg-ink-700 text-ink-100"
                      : "text-ink-400 hover:bg-ink-800"
                  }`}
                >
                  {g.label}
                </button>
              ))}
            </div>
          </div>

          {list.error && <ErrorNote message={list.error} />}
          {list.loading && !list.data && <Spinner />}
          {list.data && (
            <div className="scroll-y min-h-0 flex-1">
              {grouped.length === 0 && <Empty>Nothing matches.</Empty>}
              {grouped.map(([type, items]) => (
                <div key={type}>
                  <div className="sticky top-0 z-10 flex items-center gap-2 border-y border-ink-800/70 bg-ink-900/95 px-3 py-1 backdrop-blur">
                    <TypeChip type={type} />
                    <span className="metric text-[10px] text-ink-500">{items.length}</span>
                  </div>
                  <ul className="divide-y divide-ink-800/50">
                    {items.map((n) => (
                      <li key={n.id}>
                        <button
                          onClick={() => setSelected(n.id)}
                          className={`row-hover flex w-full items-start gap-2 px-3 py-1.5 text-left ${
                            selected === n.id ? "bg-signal/10" : ""
                          }`}
                        >
                          <span className="metric w-10 shrink-0 pt-0.5 text-[10px] text-ink-500">
                            {n.id}
                          </span>
                          <span className="min-w-0 flex-1">
                            <span className="block truncate text-xs text-ink-100">{n.label}</span>
                            {n.value && (
                              <span className="metric mt-0.5 block text-[10px] text-signal">
                                {n.value}
                              </span>
                            )}
                          </span>
                          <StatusChip status={n.status} />
                        </button>
                      </li>
                    ))}
                  </ul>
                </div>
              ))}
            </div>
          )}

          <div className="shrink-0 space-y-1.5 border-t border-ink-800/70 p-2">
            <label className="flex items-center gap-1.5 text-[10px] uppercase tracking-wider text-ink-400">
              <FileUp size={11} /> paste a source to ingest
            </label>
            <textarea
              value={draft}
              onChange={(e) => setDraft(e.target.value)}
              rows={3}
              placeholder="# Notice&#10;&#10;Battery Pack BP-7 must remain below 55 degC."
              className="w-full resize-none rounded border border-ink-700 bg-ink-900 px-2 py-1.5 font-mono text-[11px] text-ink-100 outline-none placeholder:text-ink-600 focus:border-signal/60"
            />
            <div className="flex items-center gap-2">
              <button className="btn btn-primary" onClick={submit} disabled={!draft.trim()}>
                ingest
              </button>
              {ingestNote && <span className="text-[10px] text-ink-400">{ingestNote}</span>}
            </div>
          </div>
        </div>
      </Panel>

      <Panel className="col-span-12 lg:col-span-4" title="Inspector">
        <NodeInspector
          node={detail.data ?? null}
          loading={detail.loading}
          onSelect={setSelected}
        />
      </Panel>

      <Panel className="col-span-12 lg:col-span-3" title="Temporal history">
        <Timeline timeline={timeline} onSelect={setSelected} />
      </Panel>
    </div>
  );
}
