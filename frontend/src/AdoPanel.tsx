import { type FormEvent, useCallback, useEffect, useState } from "react";
import { api, errorMessage } from "./api";
import { ConfirmedNotice } from "./ConfirmedNotice";

type Status = {
  organization: string | null;
  project: string | null;
  pat_configured: boolean;
  connected: boolean;
  error?: string | null;
  message?: string | null;
  source: string;
  checked_at: string;
};

type PullResult = {
  rows_total: number;
  upserted: number;
  ado_ids: number[];
  comments: number;
  states: Record<string, number>;
  import_batch_id?: string;
  already_confirmed_ids?: number[];
};

type Mode = "query" | "ids" | "closed";

const MODES: { value: Mode; label: string; help: string }[] = [
  { value: "query", label: "Saved query", help: "Paste the link of a saved ADO query; every work item it returns is imported." },
  { value: "ids", label: "Work item IDs", help: "Comma- or space-separated IDs, e.g. 303, 419, 450." },
  { value: "closed", label: "Closed defects", help: "All defects closed in the project between two dates." },
];

function orgName(value: string | null): string {
  if (!value) return "";
  const match = value.match(/dev\.azure\.com\/([^/]+)/);
  return match ? match[1] : value.replace(/^https:\/\//, "");
}

export function AdoPanel({
  projectId,
  csrfToken,
  isAdmin,
  onOpenAnalysis,
}: {
  projectId: string;
  csrfToken: string;
  isAdmin: boolean;
  onOpenAnalysis: (batchId?: string) => void;
}) {
  const base = `/api/v1/projects/${encodeURIComponent(projectId)}`;
  const [status, setStatus] = useState<Status | null>(null);
  const [checking, setChecking] = useState(false);
  const [organization, setOrganization] = useState("");
  const [adoProject, setAdoProject] = useState("");
  const [mode, setMode] = useState<Mode>("query");
  const [queryLink, setQueryLink] = useState("");
  const [workItemIds, setWorkItemIds] = useState("");
  const [startDate, setStartDate] = useState("");
  const [endDate, setEndDate] = useState("");
  const [includeComments, setIncludeComments] = useState(true);
  const [analyze, setAnalyze] = useState(true);
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<PullResult | null>(null);
  const [analysisNote, setAnalysisNote] = useState("");
  const [error, setError] = useState("");

  const check = useCallback(async () => {
    if (!projectId) return;
    setChecking(true);
    try {
      const next = await api<Status>(`${base}/ado-connection`);
      setStatus(next);
      setOrganization(orgName(next.organization));
      setAdoProject(next.project ?? "");
    } catch (err) {
      setStatus(null);
      setError(errorMessage(err, "Could not check the Azure DevOps connection"));
    } finally {
      setChecking(false);
    }
  }, [base, projectId]);

  // "Always connected": verify on open and every 5 minutes while the page is visible.
  useEffect(() => {
    void check();
    const timer = window.setInterval(() => {
      if (document.visibilityState === "visible") void check();
    }, 300_000);
    return () => window.clearInterval(timer);
  }, [check]);

  async function save() {
    setChecking(true);
    setError("");
    try {
      const next = await api<Status>(
        `${base}/ado-connection`,
        { method: "PUT", body: JSON.stringify({ organization, project: adoProject }) },
        csrfToken,
      );
      setStatus(next);
    } catch (err) {
      setError(errorMessage(err, "Could not connect to that organization/project"));
    } finally {
      setChecking(false);
    }
  }

  async function pull(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setBusy(true);
    setError("");
    setResult(null);
    setAnalysisNote("");
    try {
      const pulled = await api<PullResult>(
        `${base}/ado-pull`,
        {
          method: "POST",
          body: JSON.stringify({
            mode,
            query_link: mode === "query" ? queryLink : null,
            work_item_ids: mode === "ids" ? workItemIds : null,
            start_date: mode === "closed" ? startDate : null,
            end_date: mode === "closed" ? endDate : null,
            include_comments: includeComments,
          }),
        },
        csrfToken,
      );
      setResult(pulled);
      if (analyze && pulled.ado_ids.length > 0 && pulled.import_batch_id) {
        try {
          const queued = await api<{ job_id: string | null; queued: number; skipped_reviewed: number }>(
            `${base}/analyses`,
            { method: "POST", body: JSON.stringify({ import_batch_id: pulled.import_batch_id, reanalyze: true }) },
            csrfToken,
          );
          setAnalysisNote(
            queued.job_id
              ? `AI analysis queued for ${queued.queued} defect(s).`
              : "No defects needed analysis (all already have a confirmed RCA).",
          );
        } catch (err) {
          setAnalysisNote(`Imported, but analysis was not started: ${errorMessage(err, "error")}`);
        }
      }
    } catch (err) {
      setError(errorMessage(err, "Pull from Azure DevOps failed"));
    } finally {
      setBusy(false);
    }
  }

  const connected = status?.connected === true;
  const canPull =
    connected &&
    !busy &&
    ((mode === "query" && queryLink.trim()) ||
      (mode === "ids" && workItemIds.trim()) ||
      (mode === "closed" && startDate && endDate));

  return (
    <>
      <div className="card ado-card">
        <div className="card-head">
          <h2>Azure DevOps connection</h2>
          <span className={`conn-pill ${connected ? "ok" : checking ? "checking" : "down"}`} role="status">
            <span className="conn-dot" aria-hidden="true" />
            {checking && !status
              ? "Checking…"
              : connected
                ? `Connected · ${orgName(status?.organization ?? null)} / ${status?.project}`
                : "Not connected"}
          </span>
        </div>
        <div className="ado-grid">
          <label>
            Organization
            <input
              disabled={!isAdmin}
              onChange={(event) => setOrganization(event.target.value)}
              placeholder="your-org"
              value={organization}
            />
          </label>
          <label>
            Project
            <input
              disabled={!isAdmin}
              onChange={(event) => setAdoProject(event.target.value)}
              placeholder="your-project"
              value={adoProject}
            />
          </label>
        </div>
        <p className="field-help">
          Authenticates with the server&apos;s <code className="code-chip">ADO_PAT</code> (from .env). The token is
          never entered in, sent to, or shown by the browser.
          {status && !status.pat_configured && " No PAT is configured on the server yet."}
          {status?.source === "server default" && " Using the server's default organization and project."}
        </p>
        {!connected && status?.message && <p className="notice error">{status.message}</p>}
        <div className="review-buttons">
          {isAdmin && (
            <button className="primary" disabled={checking || !organization.trim() || !adoProject.trim()} onClick={() => void save()} type="button">
              {checking ? "Connecting…" : "Connect"}
            </button>
          )}
          <button className="secondary" disabled={checking} onClick={() => void check()} type="button">
            Test connection
          </button>
          {status?.checked_at && (
            <span className="muted small-print">Checked {new Date(status.checked_at).toLocaleTimeString()}</span>
          )}
        </div>
      </div>

      <form className="card ado-card" onSubmit={pull}>
        <div className="card-head">
          <h2>Pull defects</h2>
        </div>
        <fieldset className="radio-row">
          <legend>What to pull</legend>
          {MODES.map((option) => (
            <label className="radio" key={option.value} title={option.help}>
              <input checked={mode === option.value} name="ado-mode" onChange={() => setMode(option.value)} type="radio" />
              {option.label}
            </label>
          ))}
        </fieldset>
        <p className="field-help">{MODES.find((option) => option.value === mode)?.help}</p>

        <label className="switch">
          <input checked={includeComments} onChange={(event) => setIncludeComments(event.target.checked)} type="checkbox" />
          <span className="switch-track" aria-hidden="true" />
          Include every comment
        </label>
        <p className="field-help">Full discussion history; comments usually carry the root-cause evidence.</p>

        {mode === "query" && (
          <label>
            ADO query link
            <input
              onChange={(event) => setQueryLink(event.target.value)}
              placeholder="https://dev.azure.com/your-org/your-project/_queries/query/<id>"
              value={queryLink}
            />
          </label>
        )}
        {mode === "ids" && (
          <label>
            Work item IDs
            <textarea onChange={(event) => setWorkItemIds(event.target.value)} placeholder="303, 419, 450" rows={2} value={workItemIds} />
          </label>
        )}
        {mode === "closed" && (
          <div className="ado-grid">
            <label>
              Closed from
              <input onChange={(event) => setStartDate(event.target.value)} type="date" value={startDate} />
            </label>
            <label>
              Closed through
              <input onChange={(event) => setEndDate(event.target.value)} type="date" value={endDate} />
            </label>
          </div>
        )}

        <label className="switch">
          <input checked={analyze} onChange={(event) => setAnalyze(event.target.checked)} type="checkbox" />
          <span className="switch-track" aria-hidden="true" />
          Run AI analysis on the pulled defects
        </label>

        <div className="review-buttons">
          <button className="primary" disabled={!canPull} type="submit">
            {busy ? "Fetching…" : "Fetch from Azure DevOps"}
          </button>
          {!connected && <span className="muted small-print">Connect to Azure DevOps first.</span>}
        </div>

        {error && <p className="notice error">{error}</p>}
        {result && (
          <div className="notice">
            Imported <strong>{result.rows_total}</strong> work item(s)
            {result.comments ? ` with ${result.comments} comment(s)` : ""} ·{" "}
            {Object.entries(result.states)
              .map(([state, count]) => `${count} ${state}`)
              .join(", ") || "none found"}
            . {analysisNote}{" "}
            {result.import_batch_id && result.upserted > 0 && (
              <button className="text-button" onClick={() => onOpenAnalysis(result.import_batch_id)} type="button">
                {analysisNote.startsWith("AI analysis queued") ? "Open AI analysis →" : "Analyze these defects →"}
              </button>
            )}
          </div>
        )}
        {result && (
          <ConfirmedNotice
            ids={result.already_confirmed_ids ?? []}
            message="They are already in the knowledge base and will not be analyzed again."
          />
        )}
      </form>
    </>
  );
}
