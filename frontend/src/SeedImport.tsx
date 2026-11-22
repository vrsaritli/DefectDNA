import { type FormEvent, useState } from "react";
import { api, errorMessage } from "./api";

type SeedReport = {
  rows_total: number;
  rows_valid: number;
  confirmed_rows: number;
  seeded: number;
  skipped_reviewed: number;
  knowledge_status: string | null;
  rca_columns: Record<string, string>;
  unmapped: { ado_id: number; issues: { field: string; value: string }[] }[];
};

export function SeedImport({ projectId, csrfToken }: { projectId: string; csrfToken: string }) {
  const [file, setFile] = useState<File | null>(null);
  const [report, setReport] = useState<SeedReport | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!file) return;
    setBusy(true);
    setError("");
    setReport(null);
    const body = new FormData();
    body.append("file", file);
    try {
      setReport(
        await api<SeedReport>(
          `/api/v1/projects/${encodeURIComponent(projectId)}/knowledge/seed`,
          { method: "POST", body },
          csrfToken,
        ),
      );
    } catch (err) {
      setError(errorMessage(err, "Seed import failed"));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="sync-panel">
      <div>
        <p className="eyebrow">KNOWLEDGE BASE</p>
        <h2>Seed confirmed RCAs</h2>
        <p className="muted">
          Upload historical defects that already have a QA-confirmed root cause (for example a
          "Root Cause (RCA)" column). They are imported as approved RCAs and become the past
          cases the AI cites. Optional columns: "SDLC Phase (RCA)", "Testing Gap".
        </p>
      </div>
      <form className="filter-bar" onSubmit={submit}>
        <label>
          Seed file (CSV or XLSX)
          <input accept=".csv,.xlsx" onChange={(event) => setFile(event.target.files?.[0] ?? null)} required type="file" />
        </label>
        <button className="secondary" disabled={busy || !file} type="submit">
          {busy ? "Seeding…" : "Seed knowledge base"}
        </button>
      </form>
      {error && <p className="notice error">{error}</p>}
      {report && (
        <div className="notice">
          Seeded <strong>{report.seeded}</strong> confirmed RCA(s) from {report.confirmed_rows} labelled row(s)
          {report.skipped_reviewed ? `; ${report.skipped_reviewed} already reviewed and left unchanged` : ""}.
          {report.knowledge_status === "PENDING_EMBEDDING" &&
            " Entries are pending embedding until COHERE_API_KEY is configured."}
          {report.unmapped.length > 0 && (
            <>
              {" "}
              {report.unmapped.length} row(s) had labels that are not in the taxonomy:{" "}
              {report.unmapped
                .slice(0, 6)
                .map((row) => `#${row.ado_id} (${row.issues.map((issue) => issue.value).join(", ") || "no root cause"})`)
                .join("; ")}
              {report.unmapped.length > 6 ? "…" : ""}
            </>
          )}
        </div>
      )}
    </div>
  );
}
