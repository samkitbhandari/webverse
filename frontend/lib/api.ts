import type {
  Contradiction, GraphPayload, ImpactReport, KnowledgeHealth, NexusEvent,
  NodeDetail, ScenarioComparison, ScenarioResult, Stats, Timeline,
  ValidationQueue,
} from "./types";

/** Requests go to the same origin; next.config.mjs proxies /api to FastAPI. */
const BASE = "";

export class ApiError extends Error {
  constructor(message: string, readonly status: number) {
    super(message);
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    ...init,
    headers: { "Content-Type": "application/json", ...(init?.headers ?? {}) },
    cache: "no-store",
  });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = await res.json();
      detail = body?.detail ?? detail;
    } catch {
      /* non-JSON error body */
    }
    throw new ApiError(typeof detail === "string" ? detail : JSON.stringify(detail), res.status);
  }
  return res.json() as Promise<T>;
}

const qs = (params: Record<string, string | number | boolean | undefined>) => {
  const s = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v !== undefined && v !== "") s.set(k, String(v));
  }
  const out = s.toString();
  return out ? `?${out}` : "";
};

export const api = {
  stats: () => request<Stats>("/api/stats"),

  graph: (opts: { limit?: number; types?: string; include_retired?: boolean } = {}) =>
    request<GraphPayload>(`/api/graph${qs(opts)}`),

  graphAround: (id: string, depth = 2) =>
    request<GraphPayload>(`/api/graph/around/${encodeURIComponent(id)}${qs({ depth })}`),

  nodes: (opts: { type?: string; status?: string; q?: string; limit?: number; offset?: number } = {}) =>
    request<{ total: number; offset: number; items: NodeDetail[] }>(`/api/nodes${qs(opts)}`),

  node: (id: string) => request<NodeDetail>(`/api/nodes/${encodeURIComponent(id)}`),
  entity: (id: string) => request<NodeDetail>(`/api/entities/${encodeURIComponent(id)}`),
  claim: (id: string) => request<NodeDetail>(`/api/claims/${encodeURIComponent(id)}`),
  decision: (id: string) => request<NodeDetail>(`/api/decisions/${encodeURIComponent(id)}`),
  timeline: (id: string) => request<Timeline>(`/api/timeline/${encodeURIComponent(id)}`),

  contradictions: (kind?: string) =>
    request<{ count: number; items: Contradiction[] }>(`/api/contradictions${qs({ kind })}`),

  health: () => request<KnowledgeHealth>("/api/knowledge-health"),
  validationQueue: (limit = 10) => request<ValidationQueue>(`/api/validation-queue${qs({ limit })}`),

  impact: (seeds: string[], opts: { max_depth?: number; magnitudes?: Record<string, number> } = {}) =>
    request<ImpactReport>("/api/impact-analysis", {
      method: "POST",
      body: JSON.stringify({ seeds, ...opts }),
    }),

  whyAffected: (seed: string, target: string) =>
    request<{ explanation: string }>(
      `/api/impact/${encodeURIComponent(seed)}/why/${encodeURIComponent(target)}`,
    ),

  counterfactual: (body: { name: string; description?: string; interventions: unknown[] }) =>
    request<ScenarioResult>("/api/counterfactual", {
      method: "POST",
      body: JSON.stringify(body),
    }),

  compareScenarios: (scenarios: unknown[]) =>
    request<ScenarioComparison>("/api/counterfactual/compare", {
      method: "POST",
      body: JSON.stringify({ scenarios }),
    }),

  search: (q: string, k = 10) =>
    request<{ query: string; hits: any[] }>(`/api/search${qs({ q, k })}`),

  cypher: (query: string) =>
    request<{ rows: Record<string, unknown>[] }>("/api/cypher", {
      method: "POST",
      body: JSON.stringify({ query }),
    }),

  ingestText: (text: string, filename?: string) =>
    request<{ accepted: boolean; path: string }>("/api/ingest", {
      method: "POST",
      body: JSON.stringify({ text, filename }),
    }),

  recentEvents: (limit = 50) =>
    request<{ events: NexusEvent[] }>(`/api/events/recent${qs({ limit })}`),

  projectVault: () =>
    request<{ written: number; vault: string }>("/api/vault/project", { method: "POST" }),
};
