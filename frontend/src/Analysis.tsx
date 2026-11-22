import { useCallback, useEffect, useRef, useState } from "react";
import { api, errorMessage, type JobStatus, type RcaView } from "./api";

type DefectDetail = {
  ado_id: number;
  title: string;
  module: string;
  release: string;
  severity: string;
  priority: string;
  state: string;
  description: string;
  repro_steps: string;
  comments: string | { text?: string }[];
};

type AnalysisResponse = {
  job_id: string | null;
  queued: number;
  skipped_reviewed: number;
  skipped_already_analyzed: number;
  remaining: number;
};

const STATUS_LABELS: Record<RcaView["status"], string> = {
  AI_PROPOSED: "AI proposed",
  NEEDS_MANUAL_RCA: "Needs manual RCA",
  APPROVED: "Approved",
  CORRECTED: "Corrected",
};

const FAILURE_HINTS: Record<string, string> = {
  LLM_DAILY_CAP_REACHED: "The project's daily AI call limit was reached.",
  LLM_UNAVAILABLE: "The AI provider could not be reached.",
  LLM_TIMEOUT: "The AI provider timed out.",
  LLM_RATE_LIMITED: "The AI provider rate limit was hit repeatedly.",
  LLM_AUTH_FAILED: "The AI provider rejected the API key.",
  LLM_OUTPUT_TRUNCATED: "The AI answer was cut off by the token cap.",
};

function commentsText(value: DefectDetail["comments"]): string {
  if (Array.isArray(value)) return value.map((item) => item.text ?? "").join("\n\n");
  return value ?? "";
}

export function Analysis({
  projectId,
  canAnalyze,
  csrfToken,
}: {
  projectId: string;
  canAnalyze: boolean;
  csrfToken: string;
}) {
  const [rcas, setRcas] = useState<RcaView[]>([]);
  const [total, setTotal] = useState(0);
  const [statusFilter, setStatusFilter] = useState("");
  const [selected, setSelected] = useState<RcaView | null>(null);
  const [defect, setDefect] = useState<DefectDetail | null>(null);
  const [job, setJob] = useState<JobStatus | null>(null);
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const pollTimer = useRef<number | null>(null);

  const loadRcas = useCallback(async () => {
    if (!projectId) return;
    const params = new URLSearchParams({ page_size: "100" });
    if (statusFilter) params.set("status", statusFilter);
    try {
      const response = await api<{ rcas: RcaView[]; total: number }>(
        `/api/v1/projects/${encodeURIComponent(projectId)}/rcas?${params.toString()}`,
      );
      setRcas(response.rcas);
      setTotal(response.total);
    } catch (err) {
      setError(errorMessage(err, "Could not load RCAs"));
    }
  }, [projectId, statusFilter]);

  useEffect(() => {
    setSelected(null);
    setDefect(null);
    void loadRcas();
  }, [loadRcas]);

  useEffect(
    () => () => {
      if (pollTimer.current) window.clearTimeout(pollTimer.current);
    },
    [],
  );

  async function pollJob(jobId: string) {
    try {
      const status = await api<JobStatus>(`/api/v1/jobs/${encodeURIComponent(jobId)}`);
      setJob(status);
      if (status.status === "SUCCEEDED" || status.status === "FAILED") {
        await loadRcas();
        setMessage(
          status.status === "SUCCEEDED"
            ? "Analysis finished. Open a row to review the AI proposal."
            : "The analysis job failed. Items already analyzed were kept.",
        );
        return;
      }
      if (status.progress.done) void loadRcas();
    } catch (err) {
      setError(errorMessage(err, "Could not read job status"));
      return;
    }
    pollTimer.current = window.setTimeout(() => void pollJob(jobId), 2500);
  }

  async function startAnalysis(reanalyze: boolean) {
    setBusy(true);
    setError("");
    setMessage("");
    try {
      const response = await api<AnalysisResponse>(
        `/api/v1/projects/${encodeURIComponent(projectId)}/analyses`,
        { method: "POST", body: JSON.stringify({ reanalyze }) },
        csrfToken,
      );
      if (!response.job_id) {
        setMessage("No closed defects are waiting for analysis.");
        return;
      }
      setMessage(
        `Queued ${response.queued} defect(s) for AI analysis` +
          (response.remaining ? `; ${response.remaining} more will need another run.` : "."),
      );
      setJob(null);
      void pollJob(response.job_id);
    } catch (err) {
      setError(errorMessage(err, "Could not start analysis"));
    } finally {
      setBusy(false);
    }
  }

  async function openRca(rca: RcaView) {
    setSelected(rca);
    setDefect(null);
    try {
      const [detail, fresh] = await Promise.all([
        api<{ defect: DefectDetail }>(
          `/api/v1/projects/${encodeURIComponent(projectId)}/defects/${rca.defect.ado_id}`,
        ),
        api<RcaView>(`/api/v1/rcas/${encodeURIComponent(rca.id)}`),
      ]);
      setDefect(detail.defect);
      setSelected(fresh);
    } catch (err) {
      setError(errorMessage(err, "Could not load the RCA"));
    }
  }

  const running = job && (job.status === "PENDING" || job.status === "CLAIMED" || job.status === "RUNNING");
  const progressTotal = job?.progress.total ?? 0;
  const progressDone = job?.progress.done ?? 0;

  return (
    <section className="content-card">
      <div className="analysis-toolbar">
        <label>
          Status
          <select onChange={(event) => setStatusFilter(event.target.value)} value={statusFilter}>
            <option value="">All</option>
            <option value="AI_PROPOSED">AI proposed</option>
            <option value="NEEDS_MANUAL_RCA">Needs manual RCA</option>
          </select>
        </label>
        <button className="secondary" onClick={() => void loadRcas()} type="button">
          Refresh
        </button>
        {canAnalyze && (
          <div className="analysis-actions">
            <button
              className="secondary"
              disabled={busy || Boolean(running)}
              onClick={() => void startAnalysis(true)}
              title="Re-run AI analysis on closed defects that are not yet reviewed"
              type="button"
            >
              Re-analyze unreviewed
            </button>
            <button
              className="primary"
              disabled={busy || Boolean(running)}
              onClick={() => void startAnalysis(false)}
              type="button"
            >
              {running ? "Analyzing…" : "Analyze new closed defects"}
            </button>
          </div>
        )}
      </div>

      {job && (
        <div className="job-progress" aria-live="polite">
          <div className="job-progress-head">
            <strong>
              {job.status === "SUCCEEDED" ? "Analysis complete" : job.status === "FAILED" ? "Analysis failed" : "Analyzing"}
            </strong>
            <span className="muted">
              {progressDone}/{progressTotal} · {job.progress.proposed ?? 0} proposed ·{" "}
              {job.progress.needs_manual ?? 0} need manual RCA
            </span>
          </div>
          <div className="progress-track">
            <div
              className="progress-fill"
              style={{ width: `${progressTotal ? Math.round((progressDone / progressTotal) * 100) : 0}%` }}
            />
          </div>
        </div>
      )}
      {message && <p className="notice">{message}</p>}
      {error && <p className="notice error">{error}</p>}

      <div className="defect-layout">
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>ID</th>
                <th>Title</th>
                <th>Root cause</th>
                <th>Testing gap</th>
                <th>Confidence</th>
                <th>Status</th>
              </tr>
            </thead>
            <tbody>
              {rcas.map((rca) => (
                <tr
                  className={selected?.id === rca.id ? "clickable-row selected-row" : "clickable-row"}
                  key={rca.id}
                  onClick={() => void openRca(rca)}
                >
                  <td className="id-cell">#{rca.defect.ado_id}</td>
                  <td>{rca.defect.title || "—"}</td>
                  <td>{rca.proposed?.root_cause?.label ?? "—"}</td>
                  <td>{rca.proposed?.testing_gap?.label ?? "—"}</td>
                  <td>{rca.proposed ? `${Math.round(rca.proposed.confidence * 100)}%` : "—"}</td>
                  <td>
                    <span className={`rca-pill ${rca.status.toLowerCase()}`}>{STATUS_LABELS[rca.status]}</span>
                  </td>
                </tr>
              ))}
              {rcas.length === 0 && (
                <tr>
                  <td className="empty-cell" colSpan={6}>
                    No RCAs yet. {canAnalyze ? "Import closed defects, then run an analysis." : ""}
                  </td>
                </tr>
              )}
            </tbody>
          </table>
          {total > rcas.length && <p className="muted small-print">Showing {rcas.length} of {total}.</p>}
        </div>
      </div>

      {selected && (
        <div className="review-panel">
          <div className="detail-heading">
            <div>
              <p className="eyebrow">RCA REVIEW · READ-ONLY</p>
              <h2>
                #{selected.defect.ado_id} {selected.defect.title}
              </h2>
            </div>
            <button
              aria-label="Close review"
              className="icon-button"
              onClick={() => {
                setSelected(null);
                setDefect(null);
              }}
              type="button"
            >
              ×
            </button>
          </div>
          <div className="review-grid">
            <div className="review-column">
              <h3 className="column-title">Defect</h3>
              {defect ? (
                <>
                  <Field label="Module / release" value={`${defect.module || "—"} · ${defect.release || "—"}`} />
                  <Field label="Severity / priority / state" value={`${defect.severity || "—"} · ${defect.priority || "—"} · ${defect.state || "—"}`} />
                  <Field label="Description" value={defect.description} />
                  <Field label="Reproduction steps" value={defect.repro_steps} />
                  <Field label="Comments" value={commentsText(defect.comments)} />
                </>
              ) : (
                <p className="muted">Loading defect…</p>
              )}
            </div>
            <div className="review-column">
              <h3 className="column-title">AI proposal</h3>
              {selected.proposed ? (
                <>
                  <Classification label="Root cause" value={selected.proposed.root_cause} />
                  <Classification label="SDLC phase introduced" value={selected.proposed.sdlc_phase} />
                  <Classification label="Testing gap" value={selected.proposed.testing_gap} />
                  {selected.proposed.other_proposal && (
                    <Field label="Proposed new category" value={selected.proposed.other_proposal} />
                  )}
                  <div className="detail-field">
                    <h3>Confidence</h3>
                    <div className="confidence">
                      <div className="progress-track">
                        <div
                          className="progress-fill"
                          style={{ width: `${Math.round(selected.proposed.confidence * 100)}%` }}
                        />
                      </div>
                      <span>{Math.round(selected.proposed.confidence * 100)}%</span>
                    </div>
                  </div>
                  <Field label="Rationale" value={selected.proposed.rationale} />
                </>
              ) : (
                <p className="notice error">
                  The AI could not produce a valid RCA ({selected.failure_reason ?? "unknown"}).{" "}
                  {FAILURE_HINTS[selected.failure_reason ?? ""] ?? "A QA Lead needs to classify this defect manually."}
                </p>
              )}
              <p className="muted small-print">
                {selected.model?.llm ?? "model n/a"} · prompt {selected.model?.prompt_version ?? "n/a"} · taxonomy v
                {selected.taxonomy_version ?? "?"} · {selected.llm_attempts ?? 0} attempt(s) · batch of{" "}
                {selected.llm_batch?.size ?? 1}
                {selected.llm_batch?.usage?.total_tokens ? ` · ${selected.llm_batch.usage.total_tokens} tokens` : ""}
                {Object.keys(selected.scrub_counts ?? {}).length > 0 &&
                  ` · scrubbed: ${Object.entries(selected.scrub_counts)
                    .map(([kind, count]) => `${count} ${kind}`)
                    .join(", ")}`}
              </p>
            </div>
          </div>
          <p className="notice">Approve and correct actions arrive in Phase 5. This view shows the AI proposal only.</p>
        </div>
      )}
    </section>
  );
}

function Field({ label, value }: { label: string; value: string }) {
  return (
    <div className="detail-field">
      <h3>{label}</h3>
      <p className="preserve-lines">{value || "—"}</p>
    </div>
  );
}

function Classification({ label, value }: { label: string; value: { code: string; label: string } | null }) {
  return (
    <div className="detail-field">
      <h3>{label}</h3>
      <p>
        <strong>{value?.label ?? "—"}</strong> {value && <code className="code-chip">{value.code}</code>}
      </p>
    </div>
  );
}
