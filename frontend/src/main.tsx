import { type FormEvent, useEffect, useState } from "react";
import { createRoot } from "react-dom/client";
import "./style.css";
import { Analysis } from "./Analysis";
import { api, type RcaView } from "./api";

type Project = { id: string; name: string; role: string };
type Defect = {
  ado_id: number;
  title: string;
  module: string;
  release: string;
  severity: string;
  priority: string;
  state: string;
  description: string;
  repro_steps: string;
  tags: string[];
};
function App() {
  const [csrfToken, setCsrfToken] = useState("");
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [newPassword, setNewPassword] = useState("");
  const [mustChangePassword, setMustChangePassword] = useState(false);
  const [projects, setProjects] = useState<Project[]>([]);
  const [projectId, setProjectId] = useState("");
  const [screen, setScreen] = useState<"import" | "defects" | "analysis">("import");
  const [selectedRca, setSelectedRca] = useState<RcaView | null>(null);
  const [file, setFile] = useState<File | null>(null);
  const [mapping, setMapping] = useState("");
  const [result, setResult] = useState<Record<string, unknown> | null>(null);
  const [defects, setDefects] = useState<Defect[]>([]);
  const [selectedDefect, setSelectedDefect] = useState<Defect | null>(null);
  const [moduleFilter, setModuleFilter] = useState("");
  const [releaseFilter, setReleaseFilter] = useState("");
  const [startDate, setStartDate] = useState("");
  const [endDate, setEndDate] = useState("");
  const [message, setMessage] = useState("");
  const [busy, setBusy] = useState(false);

  const selectedProject = projects.find((project) => project.id === projectId);
  const canImport = selectedProject?.role === "QA_LEAD" || selectedProject?.role === "ADMIN";

  async function loadProjects(token: string) {
    const response = await api<{ projects: Project[] }>("/api/v1/projects");
    setProjects(response.projects);
    setProjectId(response.projects[0]?.id ?? "");
    setCsrfToken(token);
  }

  async function signIn(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setBusy(true);
    setMessage("");
    try {
      const response = await api<{
        csrf_token: string;
        user: { must_change_password: boolean };
      }>("/api/v1/auth/login", {
        method: "POST",
        body: JSON.stringify({ username, password }),
      });
      setCsrfToken(response.csrf_token);
      if (response.user.must_change_password) {
        setMustChangePassword(true);
        return;
      }
      await loadProjects(response.csrf_token);
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "Sign-in failed");
    } finally {
      setBusy(false);
    }
  }

  async function changePassword(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setBusy(true);
    setMessage("");
    try {
      const response = await api<{ csrf_token: string }>("/api/v1/auth/change-password", {
        method: "POST",
        body: JSON.stringify({
          current_password: password,
          new_password: newPassword,
        }),
      }, csrfToken);
      setPassword("");
      setNewPassword("");
      setMustChangePassword(false);
      await loadProjects(response.csrf_token);
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "Password change failed");
    } finally {
      setBusy(false);
    }
  }

  async function signOut() {
    setBusy(true);
    try {
      await api("/api/v1/auth/logout", { method: "POST" }, csrfToken);
      setProjects([]);
      setCsrfToken("");
      setDefects([]);
      setMessage("");
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "Sign-out failed");
    } finally {
      setBusy(false);
    }
  }

  async function uploadFile(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!file || !projectId) return;
    setBusy(true);
    setMessage("");
    const body = new FormData();
    body.append("file", file);
    if (mapping.trim()) body.append("mapping", mapping);
    try {
      const response = await api<Record<string, unknown>>(
        `/api/v1/projects/${encodeURIComponent(projectId)}/imports`,
        { method: "POST", body },
        csrfToken,
      );
      setResult(response);
      setMessage("Import finished. Review the preview and row-level errors below.");
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "Import failed");
    } finally {
      setBusy(false);
    }
  }

  async function syncAdo(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!projectId) return;
    setBusy(true);
    setMessage("");
    const params = new URLSearchParams({ start_date: startDate, end_date: endDate });
    try {
      const response = await api<Record<string, unknown>>(
        `/api/v1/projects/${encodeURIComponent(projectId)}/ado-sync?${params.toString()}`,
        { method: "POST" },
        csrfToken,
      );
      setResult(response);
      setMessage("ADO sync finished. Review the sync result below.");
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "ADO sync failed");
    } finally {
      setBusy(false);
    }
  }

  async function loadDefects(event?: FormEvent<HTMLFormElement>) {
    event?.preventDefault();
    if (!projectId) return;
    setBusy(true);
    setSelectedDefect(null);
    setMessage("");
    const params = new URLSearchParams();
    if (moduleFilter.trim()) params.set("module", moduleFilter.trim());
    if (releaseFilter.trim()) params.set("release", releaseFilter.trim());
    try {
      const response = await api<{ defects: Defect[] }>(
        `/api/v1/projects/${encodeURIComponent(projectId)}/defects?${params.toString()}`,
      );
      setDefects(response.defects);
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "Could not load defects");
    } finally {
      setBusy(false);
    }
  }

  async function showDefect(adoId: number) {
    if (!projectId) return;
    setBusy(true);
    setMessage("");
    try {
      const response = await api<{ defect: Defect; rca: RcaView | null }>(
        `/api/v1/projects/${encodeURIComponent(projectId)}/defects/${adoId}`,
      );
      setSelectedDefect(response.defect);
      setSelectedRca(response.rca);
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "Could not load defect");
    } finally {
      setBusy(false);
    }
  }

  useEffect(() => {
    if (screen === "defects" && csrfToken && projectId) void loadDefects();
  }, [screen, csrfToken, projectId]);

  if (!csrfToken || mustChangePassword) {
    return (
      <main className="signin-shell">
        <form className="signin-card" onSubmit={mustChangePassword ? changePassword : signIn}>
          <div className="brand-mark">D</div>
          <p className="eyebrow">QUALITY INTELLIGENCE</p>
          <h1>{mustChangePassword ? "Change your password" : "Welcome back"}</h1>
          <p className="muted">
            {mustChangePassword
              ? "A new password is required before you can continue."
              : "Sign in to manage project defects."}
          </p>
          {!mustChangePassword && (
            <label>
              Username
              <input
                autoComplete="username"
                onChange={(event) => setUsername(event.target.value)}
                required
                value={username}
              />
            </label>
          )}
          <label>
            {mustChangePassword ? "Current password" : "Password"}
            <input
              autoComplete="current-password"
              onChange={(event) => setPassword(event.target.value)}
              required
              type="password"
              value={password}
            />
          </label>
          {mustChangePassword && (
            <label>
              New password
              <input
                autoComplete="new-password"
                minLength={12}
                onChange={(event) => setNewPassword(event.target.value)}
                required
                type="password"
                value={newPassword}
              />
            </label>
          )}
          {message && <p className="notice error">{message}</p>}
          <button className="primary full-width" disabled={busy} type="submit">
            {busy
              ? "Please wait..."
              : mustChangePassword
                ? "Update password"
                : "Sign in"}
          </button>
        </form>
      </main>
    );
  }

  return (
    <main className="app-shell">
      <header className="topbar">
        <a aria-label="DefectDNA home" className="brand" href="#">
          <span className="brand-mark small">D</span>
          <span>DefectDNA</span>
        </a>
        <div className="topbar-actions">
          <label className="project-picker">
            <span>Project</span>
            <select onChange={(event) => setProjectId(event.target.value)} value={projectId}>
              {projects.map((project) => (
                <option key={project.id} value={project.id}>
                  {project.name}
                </option>
              ))}
            </select>
          </label>
          <span className="role-pill">{selectedProject?.role ?? "No project"}</span>
          <button className="text-button" disabled={busy} onClick={signOut} type="button">
            Sign out
          </button>
        </div>
      </header>

      <section className="page-heading">
        <div>
          <p className="eyebrow">DEFECT OPERATIONS</p>
          <h1>
            {screen === "import"
              ? "Import defects"
              : screen === "defects"
                ? "Defect register"
                : "AI root-cause analysis"}
          </h1>
          <p className="muted">
            {screen === "import"
              ? "Bring in a CSV or Excel export, validate rows, and review the import."
              : screen === "defects"
                ? "Browse imported defects and inspect their source details."
                : "Run AI analysis on closed defects and review each proposed root cause."}
          </p>
        </div>
      </section>

      <nav aria-label="Main navigation" className="tabs">
        <button
          aria-current={screen === "import" ? "page" : undefined}
          className={screen === "import" ? "tab active" : "tab"}
          onClick={() => setScreen("import")}
          type="button"
        >
          Import
        </button>
        <button
          aria-current={screen === "defects" ? "page" : undefined}
          className={screen === "defects" ? "tab active" : "tab"}
          onClick={() => setScreen("defects")}
          type="button"
        >
          Defects
        </button>
        <button
          aria-current={screen === "analysis" ? "page" : undefined}
          className={screen === "analysis" ? "tab active" : "tab"}
          onClick={() => setScreen("analysis")}
          type="button"
        >
          AI analysis
        </button>
      </nav>

      {message && <p className="notice">{message}</p>}

      {screen === "analysis" ? (
        <Analysis canAnalyze={canImport} csrfToken={csrfToken} projectId={projectId} />
      ) : screen === "import" ? (
        <section className="content-card">
          {canImport ? (
            <>
              <form className="import-form" onSubmit={uploadFile}>
            <div>
              <p className="eyebrow">FILE SOURCE</p>
              <h2>Upload a defect export</h2>
              <p className="muted">Supported formats: CSV and XLSX. Maximum file size: 10 MB.</p>
            </div>
            <label className="file-drop">
              <span className="upload-icon">↑</span>
              <strong>{file?.name ?? "Choose a file to import"}</strong>
              <span className="muted">CSV or XLSX</span>
              <input
                accept=".csv,.xlsx"
                onChange={(event) => setFile(event.target.files?.[0] ?? null)}
                required
                type="file"
              />
            </label>
            <label>
              Column mapping (optional JSON)
              <textarea
                onChange={(event) => setMapping(event.target.value)}
                placeholder={'{"Work Item ID":"ado_id","Summary":"title"}'}
                rows={3}
                value={mapping}
              />
              <span className="field-help">
                Use source column names as keys and canonical fields as values when automatic
                mapping does not recognize your export.
              </span>
            </label>
            <button className="primary" disabled={busy || !file || !projectId} type="submit">
              {busy ? "Importing..." : "Upload and import"}
            </button>
          </form>
              <div className="sync-panel">
                <div>
                  <p className="eyebrow">AZURE DEVOPS</p>
                  <h2>Sync closed defects</h2>
                  <p className="muted">Read-only sync for the configured ADO project and date range.</p>
                </div>
                <form className="filter-bar" onSubmit={syncAdo}>
                  <label>
                    Closed from
                    <input
                      onChange={(event) => setStartDate(event.target.value)}
                      required
                      type="date"
                      value={startDate}
                    />
                  </label>
                  <label>
                    Closed through
                    <input
                      onChange={(event) => setEndDate(event.target.value)}
                      required
                      type="date"
                      value={endDate}
                    />
                  </label>
                  <button className="secondary" disabled={busy || !projectId} type="submit">
                    {busy ? "Syncing..." : "Sync ADO"}
                  </button>
                </form>
              </div>
            </>
          ) : (
            <p className="notice">A QA Lead or Admin role is required to import or sync defects.</p>
          )}
          {result && (
            <div className="result-panel">
              <h3>Import report</h3>
              <pre>{JSON.stringify(result, null, 2)}</pre>
            </div>
          )}
        </section>
      ) : (
        <section className="content-card">
          <form className="filter-bar" onSubmit={loadDefects}>
            <label>
              Module
              <input
                onChange={(event) => setModuleFilter(event.target.value)}
                placeholder="All modules"
                value={moduleFilter}
              />
            </label>
            <label>
              Release
              <input
                onChange={(event) => setReleaseFilter(event.target.value)}
                placeholder="All releases"
                value={releaseFilter}
              />
            </label>
            <button className="secondary" disabled={busy} type="submit">
              Apply filters
            </button>
          </form>

          <div className="defect-layout">
            <div className="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th>ID</th>
                    <th>Title</th>
                    <th>Module</th>
                    <th>Release</th>
                    <th>State</th>
                  </tr>
                </thead>
                <tbody>
                  {defects.map((defect) => (
                    <tr
                      className="clickable-row"
                      key={defect.ado_id}
                      onClick={() => void showDefect(defect.ado_id)}
                    >
                      <td className="id-cell">#{defect.ado_id}</td>
                      <td>{defect.title}</td>
                      <td>{defect.module || "—"}</td>
                      <td>{defect.release || "—"}</td>
                      <td>
                        <span className="state-pill">{defect.state || "Unknown"}</span>
                      </td>
                    </tr>
                  ))}
                  {defects.length === 0 && (
                    <tr>
                      <td className="empty-cell" colSpan={5}>
                        No defects found for this project.
                      </td>
                    </tr>
                  )}
                </tbody>
              </table>
            </div>
            {selectedDefect && (
              <aside className="detail-panel">
                <div className="detail-heading">
                  <div>
                    <p className="eyebrow">DEFECT DETAIL</p>
                    <h2>
                      #{selectedDefect.ado_id} {selectedDefect.title}
                    </h2>
                  </div>
                  <button
                    aria-label="Close detail"
                    className="icon-button"
                    onClick={() => setSelectedDefect(null)}
                    type="button"
                  >
                    ×
                  </button>
                </div>
                <DetailField
                  label="Description"
                  value={selectedDefect.description}
                />
                <DetailField
                  label="Reproduction steps"
                  value={selectedDefect.repro_steps}
                />
                <DetailField
                  label="Severity / priority"
                  value={`${selectedDefect.severity || "—"} / ${selectedDefect.priority || "—"}`}
                />
                <DetailField
                  label="Tags"
                  value={(selectedDefect.tags ?? []).join(", ")}
                />
                <DetailField
                  label="AI root cause"
                  value={
                    selectedRca?.proposed
                      ? `${selectedRca.proposed.root_cause?.label ?? "—"} · ${Math.round(
                          selectedRca.proposed.confidence * 100,
                        )}% confidence (see AI analysis)`
                      : selectedRca
                        ? `Needs manual RCA (${selectedRca.failure_reason ?? "unknown"})`
                        : "Not analyzed yet"
                  }
                />
              </aside>
            )}
          </div>
        </section>
      )}
      <footer>DefectDNA · Phase 4 AI RCA</footer>
    </main>
  );
}

function DetailField({ label, value }: { label: string; value: string }) {
  return (
    <div className="detail-field">
      <h3>{label}</h3>
      <p>{value || "—"}</p>
    </div>
  );
}

createRoot(document.getElementById("root")!).render(<App />);
