import { ExternalLink } from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";
import { ConfirmedNotice } from "./ConfirmedNotice";
import {
  api,
  errorMessage,
  type CaseView,
  EVIDENCE_LABELS,
  type JobStatus,
  type KnowledgeStats,
  RCA_STATUS_LABELS,
  type RcaView,
  type Taxonomy,
} from "./api";

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
  skipped_reviewed_ids?: number[];
  skipped_already_analyzed: number;
  remaining: number;
  import_label?: string | null;
};

type ImportBatch = {
  id: string;
  source: string;
  label: string;
  defects: number;
  created_at: string | null;
};

function batchText(batch: ImportBatch): string {
  const kind = batch.source === "CSV_IMPORT" ? "Excel" : "Azure DevOps";
  const when = batch.created_at ? new Date(batch.created_at).toLocaleString([], { dateStyle: "medium", timeStyle: "short" }) : "";
  return `${kind} · ${batch.label} · ${batch.defects} defect${batch.defects === 1 ? "" : "s"}${when ? ` · ${when}` : ""}`;
}

function adoLink(org: string | null | undefined, project: string | null | undefined, id: number): string | null {
  if (!org || !project) return null;
  return `${org.replace(/\/$/, "")}/${encodeURIComponent(project)}/_workitems/edit/${id}`;
}

const CONFIRMED = new Set<RcaView["status"]>(["APPROVED", "CORRECTED"]);

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
  canReview,
  csrfToken,
  initialBatchId,
}: {
  projectId: string;
  canAnalyze: boolean;
  canReview: boolean;
  csrfToken: string;
  initialBatchId?: string | null;
}) {
  const [batches, setBatches] = useState<ImportBatch[]>([]);
  const [batchId, setBatchId] = useState<string>(initialBatchId ?? "");
  const [skippedIds, setSkippedIds] = useState<number[]>([]);
  const [ado, setAdo] = useState<{ connected: boolean; organization: string | null; project: string | null } | null>(null);

  useEffect(() => {
    if (!projectId) return;
    api<{ batches: ImportBatch[] }>(`/api/v1/projects/${encodeURIComponent(projectId)}/import-batches`)
      .then((response) => {
        setBatches(response.batches);
        setBatchId((current) =>
          current && response.batches.some((batch) => batch.id === current) ? current : response.batches[0]?.id ?? "",
        );
      })
      .catch(() => setBatches([]));
    if (canAnalyze) {
      api<{ connected: boolean; organization: string | null; project: string | null }>(
        `/api/v1/projects/${encodeURIComponent(projectId)}/ado-connection`,
      )
        .then(setAdo)
        .catch(() => setAdo(null));
    }
  }, [projectId, canAnalyze]);

  useEffect(() => {
    if (initialBatchId) setBatchId(initialBatchId);
  }, [initialBatchId]);
  const [taxonomy, setTaxonomy] = useState<Taxonomy | null>(null);
  const [correcting, setCorrecting] = useState(false);
  const [rejecting, setRejecting] = useState(false);
  // testing_gap_code is no longer shown; an existing value is carried over unchanged on edit.
  const [draft, setDraft] = useState<{ root_cause_code: string; sdlc_phase_code: string; testing_gap_code: string | null }>({
    root_cause_code: "",
    sdlc_phase_code: "",
    testing_gap_code: null,
  });
  const [note, setNote] = useState("");

  const [knowledge, setKnowledge] = useState<KnowledgeStats | null>(null);

  const loadKnowledge = useCallback(() => {
    if (!projectId) return;
    api<KnowledgeStats>(`/api/v1/projects/${encodeURIComponent(projectId)}/knowledge/stats`)
      .then(setKnowledge)
      .catch(() => setKnowledge(null));
  }, [projectId]);

  useEffect(() => {
    if (!projectId) return;
    api<Taxonomy>(`/api/v1/projects/${encodeURIComponent(projectId)}/taxonomy`)
      .then(setTaxonomy)
      .catch(() => setTaxonomy(null));
    loadKnowledge();
  }, [projectId, loadKnowledge]);
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
    if (batchId) params.set("import_batch_id", batchId);
    try {
      const response = await api<{ rcas: RcaView[]; total: number }>(
        `/api/v1/projects/${encodeURIComponent(projectId)}/rcas?${params.toString()}`,
      );
      setRcas(response.rcas);
      setTotal(response.total);
    } catch (err) {
      setError(errorMessage(err, "Could not load RCAs"));
    }
  }, [projectId, statusFilter, batchId]);

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
    if (!batchId) return;
    setBusy(true);
    setError("");
    setMessage("");
    setSkippedIds([]);
    try {
      const response = await api<AnalysisResponse>(
        `/api/v1/projects/${encodeURIComponent(projectId)}/analyses`,
        { method: "POST", body: JSON.stringify({ import_batch_id: batchId, reanalyze }) },
        csrfToken,
      );
      setSkippedIds(response.skipped_reviewed_ids ?? []);
      if (!response.job_id) {
        setMessage(
          response.skipped_already_analyzed
            ? `All defects in this import are already analyzed. Use “Re-analyze unreviewed” to run them again.`
            : "Nothing to analyze in this import.",
        );
        return;
      }
      setMessage(
        `Queued ${response.queued} defect(s) from “${response.import_label ?? "this import"}” for AI analysis` +
          (response.skipped_already_analyzed ? `; ${response.skipped_already_analyzed} already analyzed were skipped` : "") +
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

  function startCorrection(rca: RcaView) {
    const source = rca.final?.root_cause ? rca.final : rca.proposed;
    setDraft({
      root_cause_code: source?.root_cause?.code ?? "",
      sdlc_phase_code: source?.sdlc_phase?.code ?? "",
      testing_gap_code: source?.testing_gap?.code ?? null,
    });
    setNote(rca.final?.note ?? "");
    setCorrecting(true);
  }

  async function submitReview(decision: "APPROVE" | "CORRECT" | "REJECT") {
    if (!selected) return;
    setBusy(true);
    setError("");
    setMessage("");
    const reopen = CONFIRMED.has(selected.status);
    try {
      const updated = await api<RcaView>(
        `/api/v1/rcas/${encodeURIComponent(selected.id)}/review`,
        {
          method: "POST",
          body: JSON.stringify(
            decision === "APPROVE"
              ? { decision, reopen }
              : decision === "REJECT"
                ? { decision, reopen, note: note.trim() || null }
                : { decision, reopen, final: draft, note: note.trim() || null },
          ),
        },
        csrfToken,
      );
      setSelected(updated);
      setCorrecting(false);
      setRejecting(false);
      if (updated.status === "REJECTED") {
        setMessage(
          `#${updated.defect.ado_id} rejected. It stays out of the knowledge base and trends; correct it or re-analyze it later.`,
        );
      } else {
        const kb =
          (updated as RcaView & { knowledge_status?: string }).knowledge_status === "PENDING_EMBEDDING"
            ? " It joins the knowledge base once embeddings are configured."
            : " It is now in the knowledge base and the dashboard trends.";
        setMessage(`#${updated.defect.ado_id} ${updated.status === "APPROVED" ? "approved" : "corrected"}.${kb}`);
      }
      await loadRcas();
      loadKnowledge();
    } catch (err) {
      setError(errorMessage(err, "Review failed"));
    } finally {
      setBusy(false);
    }
  }

  async function undoReview() {
    if (!selected) return;
    setBusy(true);
    setError("");
    setMessage("");
    try {
      const updated = await api<RcaView>(
        `/api/v1/rcas/${encodeURIComponent(selected.id)}/reopen`,
        { method: "POST", body: JSON.stringify({}) },
        csrfToken,
      );
      setSelected(updated);
      setMessage(`#${updated.defect.ado_id} reopened: back to the AI proposal, out of the knowledge base and trends.`);
      await loadRcas();
      loadKnowledge();
    } catch (err) {
      setError(errorMessage(err, "Reopen failed"));
    } finally {
      setBusy(false);
    }
  }

  async function openRca(rca: RcaView) {
    setSelected(rca);
    setDefect(null);
    setCorrecting(false);
    setRejecting(false);
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
        <label className="batch-select">
          Imported defects
          <select
            aria-label="Imported defects"
            disabled={batches.length === 0}
            onChange={(event) => setBatchId(event.target.value)}
            value={batchId}
          >
            {batches.length === 0 && <option value="">No imports yet</option>}
            {batches.map((batch) => (
              <option key={batch.id} value={batch.id}>
                {batchText(batch)}
              </option>
            ))}
          </select>
        </label>
        <label>
          Status
          <select onChange={(event) => setStatusFilter(event.target.value)} value={statusFilter}>
            <option value="">All</option>
            <option value="AI_PROPOSED">AI proposed</option>
            <option value="NEEDS_MANUAL_RCA">Needs manual RCA</option>
            <option value="APPROVED">Approved</option>
            <option value="CORRECTED">Corrected</option>
            <option value="REJECTED">Rejected</option>
          </select>
        </label>
        <button className="secondary" onClick={() => void loadRcas()} type="button">
          Refresh
        </button>
        {knowledge && (
          <span
            className="kb-pill"
            title={
              knowledge.rag_enabled
                ? `Embedding model ${knowledge.current_embedding_model}`
                : "RAG is off until COHERE_API_KEY is configured"
            }
          >
            Knowledge base: {knowledge.active} confirmed case{knowledge.active === 1 ? "" : "s"}
            {knowledge.pending_embedding ? ` · ${knowledge.pending_embedding} pending` : ""}
            {!knowledge.rag_enabled && " · RAG off"}
          </span>
        )}
        {canAnalyze && (
          <div className="analysis-actions">
            <button
              className="secondary"
              disabled={busy || Boolean(running) || !batchId}
              onClick={() => void startAnalysis(true)}
              title="Re-run AI analysis on the selected import's defects that are not yet reviewed"
              type="button"
            >
              Re-analyze unreviewed
            </button>
            <button
              className="primary"
              disabled={busy || Boolean(running) || !batchId}
              onClick={() => void startAnalysis(false)}
              title="Analyze the defects from the selected Excel import or Azure DevOps query"
              type="button"
            >
              {running ? "Analyzing…" : "Analyze defects"}
            </button>
          </div>
        )}
      </div>
      {batches.length === 0 && (
        <p className="muted">
          Import defects on the Import page (Excel file or Azure DevOps query) to analyze them here.
        </p>
      )}
      <ConfirmedNotice
        ids={skippedIds}
        message="They were not sent for analysis again; their reviewed RCA stays in the knowledge base."
      />

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
                <th>AI proposed root cause</th>
                <th>ADO root cause</th>
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
                  <td>{rca.defect.ado_root_cause || "—"}</td>
                  <td>{rca.proposed ? `${Math.round(rca.proposed.confidence * 100)}%` : "—"}</td>
                  <td>
                    <span className={`rca-pill ${rca.status.toLowerCase()}`}>{RCA_STATUS_LABELS[rca.status]}</span>
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
              <p className="eyebrow">RCA REVIEW</p>
              <h2>
                #{selected.defect.ado_id} {selected.defect.title}
              </h2>
              {adoLink(ado?.organization, ado?.project, selected.defect.ado_id) && (
                <a
                  className="ado-link"
                  href={adoLink(ado?.organization, ado?.project, selected.defect.ado_id) ?? undefined}
                  rel="noopener noreferrer"
                  target="_blank"
                >
                  <ExternalLink aria-hidden="true" size={14} /> Open in Azure DevOps
                </a>
              )}
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
                  <Classification label="AI proposed root cause" value={selected.proposed.root_cause} />
                  <Field label="ADO root cause" value={selected.defect.ado_root_cause ?? ""} />
                  <Classification label="SDLC phase introduced" value={selected.proposed.sdlc_phase} />
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
                  <div className="detail-field">
                    <h3>Rationale</h3>
                    <p className="preserve-lines">{selected.proposed.rationale || "—"}</p>
                    <p className="evidence-line">
                      <span className="muted">Based on:</span>{" "}
                      {selected.proposed.evidence_fields?.length ? (
                        selected.proposed.evidence_fields.map((field) => (
                          <span className="evidence-chip" key={field}>
                            {EVIDENCE_LABELS[field] ?? field}
                          </span>
                        ))
                      ) : (
                        <span className="muted">not recorded for this proposal (re-analyze to see the sections used)</span>
                      )}
                    </p>
                  </div>
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
          <SimilarCases rca={selected} />
          {selected.status === "REJECTED" && (
            <div className="final-box rejected-box">
              <h3 className="column-title">AI proposal rejected</h3>
              <p className="muted">
                Not in the knowledge base or trends. Correct it with the right RCA, reopen it, or re-analyze it.
              </p>
              {selected.final?.note && <Field label="Reviewer note" value={selected.final.note} />}
            </div>
          )}
          {selected.final?.root_cause && (
            <div className="final-box">
              <h3 className="column-title">Confirmed RCA · {RCA_STATUS_LABELS[selected.status]}</h3>
              <div className="final-grid">
                <Classification label="Root cause" value={selected.final.root_cause} />
                <Classification label="SDLC phase introduced" value={selected.final.sdlc_phase} />
              </div>
              {selected.final.note && <Field label="Reviewer note" value={selected.final.note} />}
            </div>
          )}

          {canReview ? (
            correcting && taxonomy ? (
              <div className="review-form">
                <h3 className="column-title">Correct the RCA</h3>
                <div className="review-form-grid">
                  <CodeSelect label="Root cause" items={taxonomy.root_causes} value={draft.root_cause_code} onChange={(value) => setDraft({ ...draft, root_cause_code: value })} />
                  <CodeSelect label="SDLC phase introduced" items={taxonomy.sdlc_phases} value={draft.sdlc_phase_code} onChange={(value) => setDraft({ ...draft, sdlc_phase_code: value })} />
                </div>
                <label>
                  Note (optional)
                  <textarea maxLength={2000} onChange={(event) => setNote(event.target.value)} rows={2} value={note} />
                </label>
                <div className="review-buttons">
                  <button
                    className="primary"
                    disabled={busy || !draft.root_cause_code || !draft.sdlc_phase_code}
                    onClick={() => void submitReview("CORRECT")}
                    type="button"
                  >
                    Save confirmed RCA
                  </button>
                  <button className="text-button" onClick={() => setCorrecting(false)} type="button">
                    Cancel
                  </button>
                </div>
              </div>
            ) : rejecting ? (
              <div className="review-form">
                <h3 className="column-title">Reject the AI proposal</h3>
                <label>
                  Reason (optional)
                  <textarea maxLength={2000} onChange={(event) => setNote(event.target.value)} rows={2} value={note} />
                </label>
                <div className="review-buttons">
                  <button className="danger" disabled={busy} onClick={() => void submitReview("REJECT")} type="button">
                    Confirm reject
                  </button>
                  <button className="text-button" onClick={() => setRejecting(false)} type="button">
                    Cancel
                  </button>
                </div>
              </div>
            ) : (
              <div className="review-buttons">
                {selected.proposed && !CONFIRMED.has(selected.status) && (
                  <button className="primary" disabled={busy} onClick={() => void submitReview("APPROVE")} type="button">
                    Approve AI proposal
                  </button>
                )}
                <button className="secondary" disabled={busy || !taxonomy} onClick={() => startCorrection(selected)} type="button">
                  {CONFIRMED.has(selected.status)
                    ? "Edit confirmed RCA"
                    : selected.proposed
                      ? "Edit RCA"
                      : "Classify manually"}
                </button>
                {selected.proposed && selected.status !== "REJECTED" && (
                  <button
                    className="danger-outline"
                    disabled={busy}
                    onClick={() => {
                      setNote("");
                      setRejecting(true);
                    }}
                    type="button"
                  >
                    Reject
                  </button>
                )}
                {(CONFIRMED.has(selected.status) || selected.status === "REJECTED") && (
                  <button
                    className="text-button"
                    disabled={busy}
                    onClick={() => void undoReview()}
                    title="Back to the AI proposal; removes it from the knowledge base and trends"
                    type="button"
                  >
                    Reopen (undo review)
                  </button>
                )}
              </div>
            )
          ) : (
            <p className="notice">Only a QA Lead or Admin on this project can approve, edit or reject RCAs.</p>
          )}
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

const RETRIEVAL_NOTES: Record<string, string> = {
  DISABLED: "RAG is off (no embedding provider configured), so this proposal used no past cases.",
  EMPTY_KNOWLEDGE_BASE: "The knowledge base was empty when this defect was analyzed.",
  EMBED_DAILY_CAP_REACHED: "The daily embedding limit was reached, so no past cases were retrieved.",
};

function SimilarCases({ rca }: { rca: RcaView }) {
  const retrieved = rca.retrieved_cases ?? [];
  const cited = new Set((rca.cited_cases ?? []).map((item) => item.ref));
  const status = rca.retrieval_status ?? "";
  if (!rca.proposed && retrieved.length === 0) return null;
  return (
    <div className="cases-box">
      <h3 className="column-title">Similar confirmed cases</h3>
      {retrieved.length === 0 ? (
        <p className="muted small-print">
          {RETRIEVAL_NOTES[status] ??
            (status && status !== "OK"
              ? `Retrieval failed (${status}); the proposal used no past cases.`
              : "No confirmed case was similar enough to this defect.")}
        </p>
      ) : (
        <ul className="case-list">
          {retrieved.map((item: CaseView) => (
            <li className={cited.has(item.ref) ? "case cited" : "case"} key={item.ref}>
              <div className="case-head">
                <span className="case-id">#{item.defect_id}</span>
                <span className="case-title">{item.title || "Untitled"}</span>
                {cited.has(item.ref) && <span className="cited-pill">Cited by AI</span>}
                <span className="case-score">{item.similarity != null ? `${Math.round(item.similarity * 100)}% similar` : ""}</span>
              </div>
              <p className="case-meta">
                {item.root_cause?.label ?? "—"}
                {item.source === "SEED_IMPORT" ? " · seed import" : ""}
              </p>
              {item.summary && <p className="case-summary">{item.summary}</p>}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function CodeSelect({
  label,
  items,
  value,
  onChange,
}: {
  label: string;
  items: { code: string; label: string; definition?: string }[];
  value: string;
  onChange: (value: string) => void;
}) {
  const definition = items.find((item) => item.code === value)?.definition;
  return (
    <label>
      {label}
      <select onChange={(event) => onChange(event.target.value)} value={value}>
        <option value="">Choose…</option>
        {items.map((item) => (
          <option key={item.code} value={item.code}>
            {item.label}
          </option>
        ))}
      </select>
      {definition && <span className="field-help">{definition}</span>}
    </label>
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
