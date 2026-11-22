import { type FormEvent, useCallback, useEffect, useState } from "react";
import { api, errorMessage, type JobStatus } from "./api";

type Alert = { code: string; severity: "warning" | "critical"; message: string; value: number | null };
type Ops = {
  status: "ok" | "warning" | "critical";
  alerts: Alert[];
  metrics: {
    window_hours: number;
    analysis: {
      analyzed: number;
      needs_manual: number;
      needs_manual_rate: number | null;
      provider_errors: number;
      mean_confidence: number | null;
      llm_latency_ms: { p50: number | null; p95: number | null };
      failure_reasons: Record<string, number>;
    };
    reviews: { reviewed: number; ai_agreement_rate: number | null };
    usage_today: { llm_calls: number; llm_cap: number; embed_calls: number; embed_cap: number };
    knowledge: { active: number; pending_embedding: number; rag_enabled: boolean };
    jobs: {
      failed: number;
      stuck: number;
      failed_recent: { job_id: string; type: string; error: string | null; updated_at: string }[];
    };
    auth: { login_failures: number; lockouts: number };
  };
};

const STATUS_TEXT = { ok: "All clear", warning: "Needs attention", critical: "Action required" };

function pct(value: number | null | undefined): string {
  return value == null ? "—" : `${Math.round(value * 100)}%`;
}

export function Admin({ projectId, csrfToken }: { projectId: string; csrfToken: string }) {
  const [ops, setOps] = useState<Ops | null>(null);
  const [hours, setHours] = useState(24);
  const [error, setError] = useState("");
  const [confirm, setConfirm] = useState("");
  const [job, setJob] = useState<JobStatus | null>(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    if (!projectId) return;
    setError("");
    try {
      setOps(await api<Ops>(`/api/v1/projects/${encodeURIComponent(projectId)}/ops?hours=${hours}`));
    } catch (err) {
      setError(errorMessage(err, "Could not load operations data"));
    }
  }, [projectId, hours]);

  useEffect(() => {
    void load();
  }, [load]);

  async function pollJob(jobId: string) {
    const status = await api<JobStatus>(`/api/v1/jobs/${encodeURIComponent(jobId)}`);
    setJob(status);
    if (status.status === "RUNNING" || status.status === "PENDING") {
      window.setTimeout(() => void pollJob(jobId), 1500);
    } else {
      void load();
    }
  }

  async function deleteData(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setBusy(true);
    setError("");
    try {
      const response = await api<{ job_id: string }>(
        `/api/v1/projects/${encodeURIComponent(projectId)}/data`,
        { method: "DELETE", body: JSON.stringify({ confirm_project_id: confirm }) },
        csrfToken,
      );
      setConfirm("");
      await pollJob(response.job_id);
    } catch (err) {
      setError(errorMessage(err, "Deletion failed"));
    } finally {
      setBusy(false);
    }
  }

  const m = ops?.metrics;
  return (
    <section className="dashboard">
      <div className="dash-toolbar">
        <div className={`ops-status ${ops?.status ?? "ok"}`} role="status">
          <strong>{ops ? STATUS_TEXT[ops.status] : "Loading…"}</strong>
          <span>{ops ? `${ops.alerts.length} alert(s) in the last ${m?.window_hours} h` : ""}</span>
        </div>
        <div className="dash-actions">
          <select aria-label="Time window" value={hours} onChange={(event) => setHours(Number(event.target.value))}>
            <option value={1}>Last hour</option>
            <option value={24}>Last 24 hours</option>
            <option value={168}>Last 7 days</option>
          </select>
          <button className="secondary" onClick={() => void load()} type="button">
            Refresh
          </button>
        </div>
      </div>
      {error && <p className="notice error">{error}</p>}

      {ops && ops.alerts.length > 0 && (
        <div className="card">
          <div className="card-head">
            <h2>Alerts</h2>
          </div>
          <ul className="alert-list">
            {ops.alerts.map((alert) => (
              <li className={`alert-row ${alert.severity}`} key={alert.code}>
                <span className="alert-badge">{alert.severity === "critical" ? "Critical" : "Warning"}</span>
                <span>{alert.message}</span>
                <code className="code-chip">{alert.code}</code>
              </li>
            ))}
          </ul>
        </div>
      )}

      {m && (
        <div className="stat-grid">
          <Stat title="Analyzed" value={String(m.analysis.analyzed)} hint={`${m.analysis.needs_manual} need manual RCA (${pct(m.analysis.needs_manual_rate)})`} />
          <Stat title="AI agreement" value={pct(m.reviews.ai_agreement_rate)} hint={`${m.reviews.reviewed} QA review(s)`} />
          <Stat title="LLM calls today" value={`${m.usage_today.llm_calls} / ${m.usage_today.llm_cap}`} hint={`Embeddings ${m.usage_today.embed_calls} / ${m.usage_today.embed_cap}`} />
          <Stat
            title="LLM latency p95"
            value={m.analysis.llm_latency_ms.p95 != null ? `${(m.analysis.llm_latency_ms.p95 / 1000).toFixed(1)} s` : "—"}
            hint={`${m.analysis.provider_errors} provider error(s)`}
          />
          <Stat title="Knowledge base" value={String(m.knowledge.active)} hint={`${m.knowledge.pending_embedding} pending · RAG ${m.knowledge.rag_enabled ? "on" : "off"}`} />
          <Stat title="Failed jobs" value={String(m.jobs.failed)} hint={`${m.jobs.stuck} stuck past lease`} />
          <Stat title="Failed sign-ins" value={String(m.auth.login_failures)} hint={`${m.auth.lockouts} lockout(s), all projects`} />
          <Stat title="Mean confidence" value={pct(m.analysis.mean_confidence)} hint="AI proposals in window" />
        </div>
      )}

      {m && m.jobs.failed_recent.length > 0 && (
        <div className="card">
          <div className="card-head">
            <h2>Recent failed jobs</h2>
          </div>
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Job</th>
                  <th>Type</th>
                  <th>Error</th>
                  <th>Updated</th>
                </tr>
              </thead>
              <tbody>
                {m.jobs.failed_recent.map((item) => (
                  <tr key={item.job_id}>
                    <td className="id-cell">{item.job_id.slice(-8)}</td>
                    <td>{item.type}</td>
                    <td>{item.error ?? "—"}</td>
                    <td>{item.updated_at ? new Date(item.updated_at).toLocaleString() : "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}

      <div className="card danger-zone">
        <div className="card-head">
          <div>
            <h2>Delete project data</h2>
            <p className="muted">
              Removes this project's defects, RCAs, knowledge entries, trends and jobs from every
              service. Users, roles, the taxonomy and the audit log are kept. This cannot be undone.
            </p>
          </div>
        </div>
        <form className="filter-bar" onSubmit={deleteData}>
          <label>
            Type <strong>{projectId}</strong> to confirm
            <input autoComplete="off" onChange={(event) => setConfirm(event.target.value)} value={confirm} />
          </label>
          <button className="danger" disabled={busy || confirm !== projectId} type="submit">
            Delete project data
          </button>
        </form>
        {job && (
          <p className={job.status === "FAILED" ? "notice error" : "notice"}>
            Deletion {job.status.toLowerCase()} · {job.progress.done ?? 0}/{job.progress.total ?? 3} services
            {job.errors.length > 0 && ` · failed: ${job.errors.map((item) => item.item).join(", ")} (request again to retry)`}
          </p>
        )}
      </div>
    </section>
  );
}

function Stat({ title, value, hint }: { title: string; value: string; hint: string }) {
  return (
    <div className="card stat-card">
      <div className="stat-head">
        <span>{title}</span>
      </div>
      <div className="stat-value">{value}</div>
      <p className="stat-hint">{hint}</p>
    </div>
  );
}
