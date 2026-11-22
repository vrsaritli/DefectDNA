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

export type RcaView = {
  id: string;
  status: "AI_PROPOSED" | "NEEDS_MANUAL_RCA" | "APPROVED" | "CORRECTED";
  version: number;
  final?: { root_cause: Coded; sdlc_phase: Coded; testing_gap: Coded; confidence: number; rationale: string; other_proposal?: string | null } | null;
  cited_cases?: { id: string; defect_id: number; title?: string; final?: Record<string, unknown> }[];
  retrieved_cases?: string[];
  defect: { ado_id: number; title?: string; module?: string; release?: string; severity?: string; closed_month?: string | null };
  proposed: { root_cause: Coded; sdlc_phase: Coded; testing_gap: Coded; other_proposal: string | null; confidence: number; rationale: string } | null;
  failure_reason: string | null;
  taxonomy_version: number | null;
  model: { llm?: string; prompt_version?: string } | null;
  llm_attempts: number | null;
  llm_batch: { size?: number; usage?: Record<string, number> };
  scrub_counts: Record<string, number>;
  analyzed_at: string | null;
};

export type JobStatus = {
  job_id: string;
  type: string;
  status: "PENDING" | "CLAIMED" | "RUNNING" | "SUCCEEDED" | "FAILED";
  progress: { total?: number; done?: number; failed?: number; proposed?: number; needs_manual?: number; skipped?: number };
  errors: { item: string; message: string }[];
};
