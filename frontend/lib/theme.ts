import type { EpistemicStatus, NodeType } from "./types";

/** One colour per node type, used consistently by the graph, chips and lists. */
export const TYPE_COLOR: Record<NodeType, string> = {
  Entity: "#7aa2f7",
  Claim: "#4cc9f0",
  Observation: "#5eead4",
  Assumption: "#a78bfa",
  Decision: "#f4a261",
  Requirement: "#e879a6",
  Constraint: "#e0719c",
  Event: "#facc15",
  Risk: "#ef476f",
  Action: "#94d82d",
  Evidence: "#8792a5",
};

export const TYPE_GLYPH: Record<NodeType, string> = {
  Entity: "EN", Claim: "C", Observation: "OB", Assumption: "AS",
  Decision: "D", Requirement: "R", Constraint: "CN", Event: "EV",
  Risk: "RK", Action: "AC", Evidence: "E",
};

/** Status drives the ring around a node, not its fill. */
export const STATUS_COLOR: Record<EpistemicStatus, string> = {
  SUPPORTED: "#06d6a0",
  PROBABLE: "#4cc9f0",
  UNCERTAIN: "#f4a261",
  UNVERIFIED: "#8792a5",
  CONTESTED: "#f4a261",
  CONTRADICTED: "#ef476f",
  SUPERSEDED: "#3d4859",
  INVALIDATED: "#2b3442",
};

export const STATUS_LABEL: Record<EpistemicStatus, string> = {
  SUPPORTED: "supported",
  PROBABLE: "probable",
  UNCERTAIN: "uncertain",
  UNVERIFIED: "unverified",
  CONTESTED: "contested",
  CONTRADICTED: "contradicted",
  SUPERSEDED: "superseded",
  INVALIDATED: "invalidated",
};

export const RETIRED: EpistemicStatus[] = ["SUPERSEDED", "INVALIDATED"];

export const EDGE_COLOR: Record<string, string> = {
  SUPPORTS: "#2f7a5f",
  CONTRADICTS: "#ef476f",
  INVALIDATES: "#ef476f",
  SUPERSEDES: "#f4a261",
  DEPENDS_ON: "#4a5568",
  BASED_ON: "#5b6779",
  REQUIRES: "#5b6779",
  CONSTRAINS: "#e0719c",
  IMPLEMENTS: "#7aa2f7",
  CAUSES: "#facc15",
  AFFECTS: "#6b7688",
  ABOUT: "#2b3442",
  RELATED_TO: "#242c38",
  OWNED_BY: "#242c38",
  DERIVED_FROM: "#3d4859",
};

export const GRADE_COLOR: Record<string, string> = {
  healthy: "#06d6a0",
  watch: "#f4a261",
  degraded: "#ef8354",
  critical: "#ef476f",
};

/** Human phrasing for the canonical event stream. */
export const EVENT_LABEL: Record<string, string> = {
  CONNECTED: "stream connected",
  SOURCE_CREATED: "source detected",
  SOURCE_MODIFIED: "source changed",
  SOURCE_DELETED: "source removed",
  PIPELINE_STAGE: "pipeline",
  ENTITY_ADDED: "entity added",
  ENTITY_MERGED: "entity merged",
  CLAIM_ADDED: "claim added",
  CLAIM_CHANGED: "claim changed",
  CLAIM_SUPERSEDED: "claim superseded",
  CLAIM_INVALIDATED: "claim invalidated",
  RELATIONSHIP_ADDED: "relationship added",
  CONTRADICTION_DETECTED: "contradiction",
  CONSTRAINT_VIOLATED: "constraint violated",
  KNOWLEDGE_DRIFT: "knowledge drift",
  IMPACT_COMPUTED: "impact computed",
  DECISION_AFFECTED: "decision affected",
  GRAPH_CHANGED: "graph updated",
};

export const EVENT_TONE: Record<string, "info" | "good" | "warn" | "danger"> = {
  CONTRADICTION_DETECTED: "danger",
  CONSTRAINT_VIOLATED: "danger",
  DECISION_AFFECTED: "danger",
  CLAIM_INVALIDATED: "danger",
  KNOWLEDGE_DRIFT: "warn",
  CLAIM_CHANGED: "warn",
  CLAIM_SUPERSEDED: "warn",
  IMPACT_COMPUTED: "warn",
  SOURCE_CREATED: "good",
  SOURCE_MODIFIED: "good",
  ENTITY_ADDED: "good",
  CLAIM_ADDED: "good",
  GRAPH_CHANGED: "good",
};

export function formatNumber(n: number | null | undefined, digits = 2): string {
  if (n === null || n === undefined || Number.isNaN(n)) return "-";
  if (Math.abs(n) >= 1e9) return `${(n / 1e9).toFixed(2)}B`;
  if (Math.abs(n) >= 1e6) return `${(n / 1e6).toFixed(2)}M`;
  if (Math.abs(n) >= 1e4) return n.toLocaleString(undefined, { maximumFractionDigits: 0 });
  return n.toLocaleString(undefined, { maximumFractionDigits: digits });
}

export function timeAgo(iso?: string | null): string {
  if (!iso) return "";
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return "";
  const secs = Math.max(0, Math.round((Date.now() - then) / 1000));
  if (secs < 5) return "just now";
  if (secs < 60) return `${secs}s ago`;
  if (secs < 3600) return `${Math.floor(secs / 60)}m ago`;
  if (secs < 86400) return `${Math.floor(secs / 3600)}h ago`;
  return `${Math.floor(secs / 86400)}d ago`;
}
