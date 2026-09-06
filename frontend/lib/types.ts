export type NodeType =
  | "Entity" | "Claim" | "Decision" | "Requirement" | "Constraint"
  | "Assumption" | "Observation" | "Event" | "Risk" | "Action" | "Evidence";

export type EpistemicStatus =
  | "SUPPORTED" | "PROBABLE" | "UNCERTAIN" | "CONTESTED"
  | "CONTRADICTED" | "SUPERSEDED" | "INVALIDATED" | "UNVERIFIED";

export interface GraphNode {
  id: string;
  label: string;
  type: NodeType;
  status: EpistemicStatus;
  confidence: number;
  criticality: number;
  subject: string | null;
  predicate: string | null;
  value: string | null;
  valid_from: string | null;
  valid_until: string | null;
  /** injected client-side by the force graph */
  x?: number;
  y?: number;
}

export interface GraphLink {
  id: string;
  source: string | GraphNode;
  target: string | GraphNode;
  type: string;
  weight: number;
  confidence: number;
  rationale: string;
}

export interface GraphPayload {
  nodes: GraphNode[];
  links: GraphLink[];
  stats: Record<string, number>;
  truncated: boolean;
  focus?: string;
}

export interface Provenance {
  source: string | null;
  path: string | null;
  locator: string | null;
  excerpt: string | null;
  authority: number;
  extractor: string;
  observed_at: string | null;
}

export interface Relationship {
  type: string;
  target?: string;
  target_label?: string;
  source?: string;
  source_label?: string;
  weight: number;
  confidence: number;
  rationale: string;
}

export interface NodeDetail extends GraphNode {
  body: string;
  operator: string | null;
  value_number: number | null;
  unit: string | null;
  created_at: string;
  tags: string[];
  attributes: Record<string, unknown>;
  provenance: Provenance[];
  relationships?: { outgoing: Relationship[]; incoming: Relationship[] };
  credibility?: CredibilityBreakdown;
  timeline?: Timeline | null;
  lineage?: Lineage;
  impact_if_changed?: ImpactReport;
  claims?: NodeDetail[];
}

export interface CredibilityBreakdown {
  authority: number; recency: number; confidence: number;
  corroboration: number; specificity: number;
  temporal_validity: number; total: number;
}

export interface Contradiction {
  id: string;
  kind: "NUMERIC" | "SEMANTIC" | "TEMPORAL" | "CONSTRAINT";
  left: string; right: string;
  left_label: string; right_label: string;
  subject: string | null; predicate: string | null;
  left_value: string | null; right_value: string | null;
  description: string;
  severity: number;
  detected_at: string;
  left_credibility: CredibilityBreakdown;
  right_credibility: CredibilityBreakdown;
  winner: string | null;
  margin: number;
  verdict: string;
}

export interface ImpactHop {
  from_id: string; to_id: string;
  from_label: string; to_label: string;
  edge_type: string; influence: number;
}

export interface ImpactedNode {
  id: string; label: string; type: string; status: string;
  score: number; depth: number; criticality: number; severity: number;
  path: string[]; explanation: string; hops: ImpactHop[];
}

export interface ImpactReport {
  seeds: string[];
  seed_labels: string[];
  computed_at: string;
  affected: ImpactedNode[];
  affected_decisions: ImpactedNode[];
  max_depth_reached: number;
  total_affected: number;
  notes: string[];
}

export interface TimelinePoint {
  node_id: string; label: string; value: string | null;
  status: EpistemicStatus; confidence: number;
  valid_from: string | null; valid_until: string | null;
  created_at: string; source: string | null;
}

export interface Timeline {
  subject: string; predicate: string; points: TimelinePoint[];
}

export interface LineageBasis {
  id: string; label: string; type: string; relationship: string;
  believed_at_decision_time: boolean;
  status_then: string; status_now: string;
  value_now: string | null; changed: boolean;
}

export interface Lineage {
  decision: { id: string; label: string; status: string; taken_at: string };
  basis: LineageBasis[];
  changed_since: LineageBasis[];
  still_valid: boolean;
  summary: string;
}

export interface KnowledgeHealth {
  computed_at: string;
  score: number;
  grade: "healthy" | "watch" | "degraded" | "critical";
  nodes: number; edges: number;
  by_type: Record<string, number>;
  by_status: Record<string, number>;
  average_confidence: number;
  total_uncertainty: number;
  open_contradictions: number;
  contradiction_severity: number;
  contested: number; superseded: number;
  expired_beliefs: number; unsourced_claims: number;
  orphan_nodes: number;
  decisions_without_lineage: number;
  decisions_at_risk: number;
  issues: string[];
  critical_nodes: {
    id: string; label: string; type: string; status: string;
    criticality: number; dependents: number; reason: string;
  }[];
  centrality: Record<string, { id: string; label: string; type: string; score: number }[]>;
}

export interface ValidationCandidate {
  id: string; label: string; type: string; status: string;
  confidence: number; criticality: number;
  downstream_nodes: number; downstream_decisions: number;
  uncertainty_reduction: number; uncertainty_reduction_pct: number;
  reason: string; suggested_action: string;
}

export interface ValidationQueue {
  computed_at: string;
  total_uncertainty: number;
  open_questions: number;
  candidates: ValidationCandidate[];
  summary: string;
}

export interface DimensionDelta {
  name: string;
  before: number | null;
  after: number | null;
  delta: number | null;
  direction: "better" | "worse" | "unchanged" | "unknown";
  note: string;
}

export interface ScenarioResult {
  scenario: string;
  description: string;
  computed_at: string;
  interventions: string[];
  baseline: Record<string, number | null>;
  counterfactual: Record<string, number | null>;
  deltas: DimensionDelta[];
  first_order: { id: string; label: string; type: string; severity: number; depth: number; explanation: string }[];
  second_order: { id: string; label: string; type: string; severity: number; depth: number; explanation: string }[];
  newly_at_risk: { id: string; label: string }[];
  resolved_decisions: { id: string; label: string }[];
  new_contradictions: Contradiction[];
  resolved_contradictions: Contradiction[];
  verdict: string;
}

export interface ScenarioComparison {
  comparison: {
    ranking: {
      scenario: string; score: number; health: number;
      new_contradictions: number; resolved_contradictions: number;
      decisions_at_risk: number; risk_exposure: number; verdict: string;
    }[];
    recommended: string | null;
    rationale: string;
  };
  scenarios: ScenarioResult[];
}

export interface NexusEvent {
  event_id?: string;
  event_type: string;
  source_id?: string | null;
  timestamp?: string;
  payload: Record<string, any>;
  replayed?: boolean;
}

export interface Stats {
  graph: Record<string, number>;
  vectors: number;
  worker: { processed: number; failed: number; running: boolean; last_result: any };
  subscribers: number;
  providers: { llm: string; embedder: string };
  paths: { incoming: string; vault: string; data: string };
}
