type ApiError = { error?: { message?: string; code?: string } };

export async function api<T>(path: string, options: RequestInit = {}, csrfToken?: string): Promise<T> {
  const headers = new Headers(options.headers);
  if (csrfToken) headers.set("X-CSRF-Token", csrfToken);
  if (options.body && !(options.body instanceof FormData)) headers.set("Content-Type", "application/json");
  const response = await fetch(path, { ...options, headers, credentials: "include" });
  const raw = await response.text();
  const payload = (raw ? JSON.parse(raw) : {}) as T & ApiError;
  if (!response.ok) throw new Error(payload.error?.message ?? payload.error?.code ?? `Request failed (${response.status})`);
  return payload;
}

export function errorMessage(error: unknown, fallback: string): string {
  return error instanceof Error ? error.message : fallback;
}

export type Coded = { code: string; label: string } | null;

export type CaseView = {
  ref: string;
  knowledge_entry_id?: string;
  defect_id: number;
  similarity: number | null;
  title?: string;
  module?: string;
  summary?: string;
  source?: string;
  root_cause: Coded;
  sdlc_phase: Coded;
  testing_gap: Coded;
};

export type KnowledgeStats = {
  total: number;
  active: number;
  pending_embedding: number;
  by_source: Record<string, number>;
  rag_enabled: boolean;
  current_embedding_model: string;
};

export type RcaStatus = "AI_PROPOSED" | "NEEDS_MANUAL_RCA" | "DUPLICATE" | "APPROVED" | "CORRECTED" | "REJECTED";

export const RCA_STATUS_LABELS: Record<RcaStatus, string> = {
  AI_PROPOSED: "AI proposed",
  NEEDS_MANUAL_RCA: "Needs manual RCA",
  DUPLICATE: "Duplicate",
  APPROVED: "Approved",
  CORRECTED: "Corrected",
  REJECTED: "Rejected",
};

/** Names of the defect sections an AI proposal can cite as its evidence. */
export const EVIDENCE_LABELS: Record<string, string> = {
  title: "Title",
  description: "Description",
  repro_steps: "Reproduction steps",
  comments: "Comments",
  tags: "Tags",
  module: "Module",
  release: "Release",
  severity: "Severity",
  priority: "Priority",
  similar_cases: "Similar confirmed cases",
};

/** Root cause that QA could have caught before release. */
export const QA_CATCHABLE_ROOT_CAUSE = "CODE";

/**
 * "Could have been caught by QA?": "Y - <testing gap>" when the root cause is Code,
 * otherwise "N - <root cause>". Same rule as the RCA export.
 */
export function qaCatchText(
  value: { root_cause?: Coded | null; testing_gap?: Coded | null } | null | undefined,
  status?: string,
): string {
  if (status === "DUPLICATE") return "N - Duplicate";
  const root = value?.root_cause;
  if (!root) return "—";
  if (root.code === QA_CATCHABLE_ROOT_CAUSE) return `Y - ${value?.testing_gap?.label ?? "—"}`;
  return `N - ${root.label}`;
}

/** Root cause cell: "N/A (duplicate of #608)" for duplicates, else the label. */
export function rootCauseText(root: Coded | null | undefined, status?: string, duplicateOf?: number | null): string {
  if (status === "DUPLICATE") return duplicateOf ? `N/A (duplicate of #${duplicateOf})` : "N/A (duplicate)";
  return root?.label ?? "—";
}

export function evidenceText(fields: string[]): string {
  return fields.map((field) => EVIDENCE_LABELS[field] ?? field).join(", ");
}

export type RcaView = {
  id: string;
  status: RcaStatus;
  version: number;
  final?: {
    root_cause: Coded | null;
    sdlc_phase: Coded | null;
    testing_gap: Coded | null;
    note?: string | null;
  } | null;
  reviewed_at?: string | null;
  cited_cases?: CaseView[];
  retrieved_cases?: CaseView[];
  retrieval_status?: string | null;
  review_source?: "REVIEW" | "SEED_IMPORT" | null;
  defect: {
    ado_id: number;
    title?: string;
    module?: string;
    release?: string;
    severity?: string;
    closed_month?: string | null;
    /** Root cause recorded in Azure DevOps (from the import), if any. */
    ado_root_cause?: string | null;
  };
  proposed: {
    root_cause: Coded;
    sdlc_phase: Coded;
    testing_gap: Coded;
    other_proposal: string | null;
    confidence: number;
    rationale: string;
    /** Defect sections the AI based the root cause on (prompt rca-v3 and later). */
    evidence_fields?: string[];
  } | null;
  failure_reason: string | null;
  /** Original defect when this one was closed as a duplicate (status DUPLICATE). */
  duplicate_of?: number | null;
  taxonomy_version: number | null;
  model: { llm?: string; prompt_version?: string } | null;
  llm_attempts: number | null;
  llm_batch: { size?: number; usage?: Record<string, number> };
  scrub_counts: Record<string, number>;
  analyzed_at: string | null;
};

export type TaxonomyItem = { code: string; label: string; definition?: string };
export type Taxonomy = {
  version: number;
  root_causes: TaxonomyItem[];
  sdlc_phases: TaxonomyItem[];
  testing_gaps: TaxonomyItem[];
};

export type JobStatus = {
  job_id: string;
  type: string;
  status: "PENDING" | "CLAIMED" | "RUNNING" | "SUCCEEDED" | "FAILED";
  progress: { total?: number; done?: number; failed?: number; proposed?: number; needs_manual?: number; skipped?: number };
  errors: { item: string; message: string }[];
};
