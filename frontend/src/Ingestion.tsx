import { Database, FileSpreadsheet, GitBranch, UploadCloud } from "lucide-react";
import { type FormEvent, useCallback, useEffect, useState } from "react";
import { api, errorMessage, type KnowledgeStats } from "./api";
import { ConfirmedNotice } from "./ConfirmedNotice";

type Source = "excel" | "ado";
type Report = {
  rows_total: number;
  confirmed_rows: number;
  seeded: number;
  skipped_reviewed: number;
  already_confirmed_ids?: number[];
  knowledge_status: string | null;
  rca_columns: Record<string, string>;
  unmapped: { ado_id: number; issues: { field: string; value: string }[] }[];
  source: string;
};
type AdoStatus = { connected: boolean; organization: string | null; project: string | null; message?: string | null };

function orgName(value: string | null): string {
  const match = value?.match(/dev\.azure\.com\/([^/]+)/);
  return match ? match[1] : (value ?? "").replace(/^https:\/\//, "");
}

export function Ingestion({ projectId, csrfToken }: { projectId: string; csrfToken: string }) {
  const base = `/api/v1/projects/${encodeURIComponent(projectId)}`;
  const [source, setSource] = useState<Source>("excel");
  const [file, setFile] = useState<File | null>(null);
  const [queryLink, setQueryLink] = useState("");
  const [includeComments, setIncludeComments] = useState(true);
  const [rcaField, setRcaField] = useState("");
  const [stats, setStats] = useState<KnowledgeStats | null>(null);
  const [ado, setAdo] = useState<AdoStatus | null>(null);
  const [report, setReport] = useState<Report | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  const loadStats = useCallback(() => {
    api<KnowledgeStats>(`${base}/knowledge/stats`).then(setStats).catch(() => setStats(null));
  }, [base]);

  useEffect(() => {
    loadStats();
    api<AdoStatus>(`${base}/ado-connection`).then(setAdo).catch(() => setAdo(null));
  }, [base, loadStats]);

  async function ingest(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setBusy(true);
    setError("");
    setReport(null);
    try {
      let result: Report;
      if (source === "excel") {
        if (!file) return;
        const body = new FormData();
        body.append("file", file);
        result = await api<Report>(`${base}/knowledge/seed`, { method: "POST", body }, csrfToken);
      } else {
        result = await api<Report>(
          `${base}/knowledge/seed-ado`,
          {
            method: "POST",
            body: JSON.stringify({
              mode: "query",
              query_link: queryLink,
              include_comments: includeComments,
              rca_field: rcaField.trim() || null,
            }),
          },
          csrfToken,
        );
      }
      setReport(result);
      loadStats();
    } catch (err) {
      setError(errorMessage(err, "Ingestion failed"));
    } finally {
      setBusy(false);
    }
  }

  const ready = source === "excel" ? Boolean(file) : Boolean(queryLink.trim()) && ado?.connected === true;
  const bySource = stats?.by_source ?? {};

  return (
    <section className="dashboard">
      <div className="stat-grid">
        <Stat title="Knowledge entries" value={stats ? String(stats.active) : "—"} hint="Confirmed RCAs the AI can cite" />
        <Stat title="From ingestion" value={String(bySource.SEED_IMPORT ?? 0)} hint="Loaded from Excel or ADO" />
        <Stat title="From QA reviews" value={String(bySource.REVIEW ?? 0)} hint="Approved or corrected in the app" />
        <Stat
          title="Pending embedding"
          value={stats ? String(stats.pending_embedding) : "—"}
          hint={stats?.rag_enabled ? `RAG on · ${stats.current_embedding_model}` : "RAG off: no embedding key"}
        />
      </div>

      <form className="card ingest-card" onSubmit={ingest}>
        <div className="card-head">
          <div>
            <h2>Ingest confirmed RCAs into the knowledge base</h2>
            <p className="muted">
              Historical defects with a QA-confirmed root cause are stored in MongoDB as approved RCAs
              and embedded, so new analyses can cite them. Admin only.
            </p>
          </div>
          <span className="kb-pill">
            <Database aria-hidden="true" size={14} /> MongoDB knowledge base
          </span>
        </div>

        <fieldset className="source-picker">
          <legend>Ingestion source</legend>
          <label className={source === "excel" ? "source-option selected" : "source-option"}>
            <input checked={source === "excel"} name="ingest-source" onChange={() => setSource("excel")} type="radio" />
            <FileSpreadsheet aria-hidden="true" size={22} />
            <span>
              <b>Excel file</b>
              <span className="muted">Upload an XLSX or CSV export with a confirmed root-cause column</span>
            </span>
          </label>
          <label className={source === "ado" ? "source-option selected" : "source-option"}>
            <input checked={source === "ado"} name="ingest-source" onChange={() => setSource("ado")} type="radio" />
            <GitBranch aria-hidden="true" size={22} />
            <span>
              <b>Azure DevOps query</b>
              <span className="muted">Run a saved ADO query and read each defect&apos;s RCA field</span>
            </span>
          </label>
        </fieldset>

        {source === "excel" ? (
          <>
            <label className="file-drop">
              <span className="upload-icon">
                <UploadCloud aria-hidden="true" size={20} />
              </span>
              <strong>{file?.name ?? "Click to choose a file"}</strong>
              <span className="muted">Required columns: ID, Title, Root Cause (RCA)</span>
              <input accept=".csv,.xlsx" onChange={(event) => setFile(event.target.files?.[0] ?? null)} type="file" />
            </label>
            <p className="field-help">
              Optional columns: “SDLC Phase (RCA)” and “Testing Gap”. A plain “SDLC Phase” column is ignored: in ADO
              exports it records where a defect was found, not where it was introduced.
            </p>
          </>
        ) : (
          <>
            <div className="ingest-ado-status">
              <span className={`conn-pill ${ado?.connected ? "ok" : "down"}`}>
                <span className="conn-dot" aria-hidden="true" />
                {ado?.connected ? `Connected · ${orgName(ado.organization)} / ${ado.project}` : "Not connected"}
              </span>
              {!ado?.connected && (
                <span className="muted small-print">{ado?.message ?? "Set up the connection on the Import page."}</span>
              )}
            </div>
            <label>
              ADO query link
              <input
                onChange={(event) => setQueryLink(event.target.value)}
                placeholder="https://dev.azure.com/your-org/your-project/_queries/query/<id>"
                value={queryLink}
              />
            </label>
            <div className="ado-grid">
              <label>
                Root-cause field (optional)
                <input
                  onChange={(event) => setRcaField(event.target.value)}
                  placeholder="Root Cause (RCA) / RCA"
                  value={rcaField}
                />
                <span className="field-help">Leave empty to detect it automatically.</span>
              </label>
              <label className="switch ingest-switch">
                <input checked={includeComments} onChange={(event) => setIncludeComments(event.target.checked)} type="checkbox" />
                <span className="switch-track" aria-hidden="true" />
                Include every comment
              </label>
            </div>
          </>
        )}

        <div className="review-buttons">
          <button className="primary" disabled={busy || !ready} type="submit">
            <Database aria-hidden="true" size={16} />
            {busy ? "Ingesting…" : "Ingest into knowledge base"}
          </button>
        </div>
        {error && <p className="notice error">{error}</p>}
      </form>

      {report && (
        <div className="card">
          <div className="card-head">
            <div>
              <h2>Ingestion report</h2>
              <p className="muted">
                Source: {report.source === "ADO_QUERY" ? "Azure DevOps query" : "Excel file"} · root-cause field “
                {report.rca_columns.root_cause ?? "—"}”
              </p>
            </div>
          </div>
          <div className="result-stats">
            <Result value={report.rows_total} label="Defects read" />
            <Result value={report.confirmed_rows} label="With confirmed RCA" />
            <Result value={report.seeded} label="Ingested" />
            <Result value={report.skipped_reviewed} label="Already confirmed (skipped)" />
            <Result value={report.unmapped.length} label="Labels not in taxonomy" />
          </div>
          <ConfirmedNotice
            ids={report.already_confirmed_ids ?? []}
            message="They are already in the knowledge base, so they were not ingested again."
          />
          {report.knowledge_status === "PENDING_EMBEDDING" && (
            <p className="notice">Entries are stored and will be embedded once an embedding key is configured.</p>
          )}
          {report.unmapped.length > 0 && (
            <div className="table-wrap ingest-unmapped">
              <table>
                <thead>
                  <tr>
                    <th>Defect</th>
                    <th>Label not in the taxonomy</th>
                  </tr>
                </thead>
                <tbody>
                  {report.unmapped.map((row) => (
                    <tr key={row.ado_id}>
                      <td className="id-cell">#{row.ado_id}</td>
                      <td>{row.issues.map((issue) => `${issue.field}: ${issue.value}`).join(" · ") || "No root cause"}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      )}
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

function Result({ value, label }: { value: number; label: string }) {
  return (
    <div className="result-stat">
      <b>{value}</b>
      <span>{label}</span>
    </div>
  );
}
