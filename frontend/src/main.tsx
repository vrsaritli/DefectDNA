import {
  BrainCircuit,
  ChevronRight,
  Database,
  Dna,
  FileUp,
  LayoutDashboard,
  ListChecks,
  LogOut,
  Presentation,
  ShieldCheck,
  Sparkles,
  TrendingUp,
  UploadCloud,
  Workflow,
  Wrench,
  type LucideIcon,
} from "lucide-react";
import { type FormEvent, useEffect, useState } from "react";
import { createRoot } from "react-dom/client";
import "./style.css";
import { AdoPanel } from "./AdoPanel";
import { Admin } from "./Admin";
import { Analysis } from "./Analysis";
import { ConfirmedNotice } from "./ConfirmedNotice";
import { Dashboard } from "./Dashboard";
import { Ingestion } from "./Ingestion";
import { api, type Coded, evidenceText, RCA_STATUS_LABELS, type RcaStatus, type RcaView } from "./api";

type Screen = "dashboard" | "import" | "defects" | "analysis" | "ingestion" | "admin";
const SCREENS: Record<Screen, { label: string; title: string; description: string; icon: LucideIcon }> = {
  dashboard: {
    label: "Dashboard",
    title: "Root-cause trends",
    description: "Recurring root causes by module, release and month, from reviewed RCAs.",
    icon: LayoutDashboard,
  },
  import: {
    label: "Import",
    title: "Import defects",
    description: "Upload an export or pull straight from Azure DevOps.",
    icon: FileUp,
  },
  defects: {
    label: "Defects",
    title: "Defect register",
    description: "Browse imported defects and inspect their source details.",
    icon: ListChecks,
  },
  analysis: {
    label: "AI analysis",
    title: "AI analysis and review",
    description: "Analyze the defects from an Excel import or Azure DevOps query, then approve, correct or reject each proposed root cause.",
    icon: BrainCircuit,
  },
  ingestion: {
    label: "Ingestion",
    title: "Knowledge ingestion",
    description: "Load historical defects with confirmed root causes into the MongoDB knowledge base.",
    icon: Database,
  },
  admin: {
    label: "Admin",
    title: "Operations",
    description: "Pipeline health, alerts and project data management.",
    icon: Wrench,
  },
};
const ROLE_LABEL: Record<string, string> = { ADMIN: "Admin", QA_LEAD: "QA Lead", VIEWER: "Viewer" };

type Project = { id: string; name: string; role: string };
type Me = { username: string; display_name: string };
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
  ado_root_cause?: string | null;
  rca?: RcaSummary | null;
};
type RcaSummary = {
  id: string;
  status: RcaStatus;
  review_source?: string | null;
  root_cause: Coded;
  sdlc_phase: Coded;
  testing_gap: Coded;
  confidence: number | null;
};

function rcaStatusText(rca: RcaSummary): string {
  if (rca.review_source === "SEED_IMPORT" && rca.status === "APPROVED") return "Confirmed (ingested)";
  return RCA_STATUS_LABELS[rca.status] ?? rca.status;
}
type ImportResult = {
  import_batch_id?: string;
  upserted?: number;
  already_confirmed_ids?: number[];
  preview?: { rows_total?: number; rows_valid?: number; row_errors?: unknown[] };
};

function initials(name: string): string {
  const parts = name.trim().split(/[\s._@-]+/).filter(Boolean);
  return ((parts[0]?.[0] ?? "") + (parts[1]?.[0] ?? "")).toUpperCase() || "?";
}

function App() {
  const [csrfToken, setCsrfToken] = useState("");
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [newPassword, setNewPassword] = useState("");
  const [mustChangePassword, setMustChangePassword] = useState(false);
  const [me, setMe] = useState<Me | null>(null);
  const [projects, setProjects] = useState<Project[]>([]);
  const [projectId, setProjectId] = useState("");
  const [screen, setScreen] = useState<Screen>("dashboard");
  const [selectedRca, setSelectedRca] = useState<RcaView | null>(null);
  const [file, setFile] = useState<File | null>(null);
  const [mapping, setMapping] = useState("");
  const [result, setResult] = useState<ImportResult | null>(null);
  const [analysisBatch, setAnalysisBatch] = useState<string | null>(null);
  const [defects, setDefects] = useState<Defect[]>([]);
  const [selectedDefect, setSelectedDefect] = useState<Defect | null>(null);
  const [moduleFilter, setModuleFilter] = useState("");
  const [releaseFilter, setReleaseFilter] = useState("");
  const [message, setMessage] = useState("");
  const [busy, setBusy] = useState(false);

  const selectedProject = projects.find((project) => project.id === projectId);
  const canImport = selectedProject?.role === "QA_LEAD" || selectedProject?.role === "ADMIN";

  async function loadProjects(token: string) {
    const [response, profile] = await Promise.all([
      api<{ projects: Project[] }>("/api/v1/projects"),
      api<{ user: Me }>("/api/v1/me").catch(() => null),
    ]);
    setProjects(response.projects);
    setProjectId(response.projects[0]?.id ?? "");
    setMe(profile?.user ?? { username, display_name: username });
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
      const response = await api<{ csrf_token: string }>(
        "/api/v1/auth/change-password",
        {
          method: "POST",
          body: JSON.stringify({ current_password: password, new_password: newPassword }),
        },
        csrfToken,
      );
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
      setMe(null);
      setPassword("");
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
      const response = await api<ImportResult>(
        `/api/v1/projects/${encodeURIComponent(projectId)}/imports`,
        { method: "POST", body },
        csrfToken,
      );
      setResult(response);
      setMessage("Import finished. Review the summary and any row-level errors below.");
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "Import failed");
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
      setSelectedDefect({ ...response.defect, rca: defects.find((item) => item.ado_id === adoId)?.rca ?? null });
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

  useEffect(() => {
    setMessage("");
  }, [screen]);

  if (!csrfToken || mustChangePassword) {
    return (
      <main className="signin-shell">
        <aside className="signin-aside" aria-hidden="true">
          <span className="brand">
            <span className="brand-mark">
              <Dna size={18} />
            </span>
            DefectDNA
          </span>
          <div className="signin-pitch">
            <h2>AI root-cause analysis that learns from every defect</h2>
            <p>Consistent RCA for every closed Azure DevOps defect, confirmed by your QA Leads.</p>
            <ul className="signin-points">
              <li>
                <Sparkles size={18} /> Root cause and SDLC phase in minutes
              </li>
              <li>
                <Workflow size={18} /> Grounded in your own confirmed history
              </li>
              <li>
                <TrendingUp size={18} /> Recurring patterns surfaced before production
              </li>
              <li>
                <ShieldCheck size={18} /> Sensitive data scrubbed before any AI call
              </li>
            </ul>
          </div>
          <p className="signin-foot">Quality intelligence for QA teams</p>
        </aside>
        <div className="signin-main">
          <form className="signin-card" onSubmit={mustChangePassword ? changePassword : signIn}>
            <span className="brand-mark" aria-hidden="true">
              <Dna size={18} />
            </span>
            <h1>{mustChangePassword ? "Change your password" : "Welcome back"}</h1>
            <p className="muted">
              {mustChangePassword
                ? "A new password is required before you can continue."
                : "Sign in to your DefectDNA workspace."}
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
                <span className="field-help">
                  At least 12 characters, with three of: lowercase, uppercase, digits, symbols.
                </span>
              </label>
            )}
            {message && <p className="notice error">{message}</p>}
            <button className="primary full-width" disabled={busy} type="submit">
              {busy ? "Please wait…" : mustChangePassword ? "Update password" : "Sign in"}
            </button>
          </form>
        </div>
      </main>
    );
  }

  const workspace: Screen[] = ["dashboard", "import", "defects", "analysis"];
  const current = SCREENS[screen];
  const displayName = me?.display_name || me?.username || username;

  const navButton = (id: Screen) => {
    const Icon = SCREENS[id].icon;
    return (
      <button
        aria-current={screen === id ? "page" : undefined}
        className={screen === id ? "nav-item active" : "nav-item"}
        key={id}
        onClick={() => setScreen(id)}
        type="button"
      >
        <Icon aria-hidden="true" size={18} />
        {SCREENS[id].label}
      </button>
    );
  };

  return (
    <div className="app-shell">
      <aside className="sidebar">
        <a aria-label="DefectDNA home" className="brand" href="#" onClick={() => setScreen("dashboard")}>
          <span className="brand-mark">
            <Dna aria-hidden="true" size={18} />
          </span>
          <span>
            DefectDNA
            <span className="brand-sub">RCA intelligence</span>
          </span>
        </a>

        <label className="project-switch">
          <span>Project</span>
          <select onChange={(event) => setProjectId(event.target.value)} value={projectId}>
            {projects.map((project) => (
              <option key={project.id} value={project.id}>
                {project.name}
              </option>
            ))}
          </select>
        </label>

        <p className="sidebar-label">Workspace</p>
        <nav aria-label="Main navigation" className="nav">
          {workspace.map(navButton)}
        </nav>
        {selectedProject?.role === "ADMIN" && (
          <>
            <p className="sidebar-label">Administration</p>
            <nav aria-label="Administration" className="nav">
              {navButton("ingestion")}
              {navButton("admin")}
            </nav>
          </>
        )}

        <div className="sidebar-foot">
          <div className="user-card">
            <span className="avatar" aria-hidden="true">
              {initials(displayName)}
            </span>
            <div className="user-meta">
              <p className="user-name" title={displayName}>
                {displayName}
              </p>
              <span className="role-pill">
                {ROLE_LABEL[selectedProject?.role ?? ""] ?? "No project"}
              </span>
            </div>
            <button aria-label="Sign out" className="signout" disabled={busy} onClick={signOut} title="Sign out" type="button">
              <LogOut aria-hidden="true" size={18} />
            </button>
          </div>
          <p className="build-tag">
            DefectDNA · v1.0 ·{" "}
            <a href="/DefectDNA-demo-slides.pdf" rel="noopener" target="_blank" title="Open the demo slides (PDF)">
              <Presentation aria-hidden="true" size={12} />
              Demo slides
            </a>
          </p>
        </div>
      </aside>

      <div className="main">
        <header className="page-header">
          <div>
            <nav aria-label="Breadcrumb" className="breadcrumb">
              <span>{selectedProject?.name ?? "No project"}</span>
              <ChevronRight aria-hidden="true" size={14} />
              <span>{current.label}</span>
            </nav>
            <h1>{current.title}</h1>
            <p className="muted">{current.description}</p>
          </div>
        </header>

        <main className="page-body">
          {message && <p className="notice">{message}</p>}

          {screen === "ingestion" && selectedProject?.role === "ADMIN" ? (
            <Ingestion csrfToken={csrfToken} projectId={projectId} />
          ) : screen === "admin" ? (
            <Admin csrfToken={csrfToken} projectId={projectId} />
          ) : screen === "dashboard" ? (
            <Dashboard canRebuild={canImport} csrfToken={csrfToken} projectId={projectId} />
          ) : screen === "analysis" ? (
            <Analysis
              canAnalyze={canImport}
              canReview={selectedProject?.role === "QA_LEAD" || selectedProject?.role === "ADMIN"}
              csrfToken={csrfToken}
              initialBatchId={analysisBatch}
              projectId={projectId}
            />
          ) : screen === "import" ? (
            canImport ? (
              <div className="import-grid">
                <div className="import-col">
                  <form className="card import-form" onSubmit={uploadFile}>
                    <div className="card-head">
                      <div>
                        <h2>Upload a defect export</h2>
                        <p className="muted">CSV or XLSX from Azure DevOps, up to 10 MB.</p>
                      </div>
                    </div>
                    <label className="file-drop">
                      <span className="upload-icon">
                        <UploadCloud aria-hidden="true" size={20} />
                      </span>
                      <strong>{file?.name ?? "Click to choose a file"}</strong>
                      <span className="muted">CSV or XLSX · ID and Title columns required</span>
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
                        Only needed when automatic mapping does not recognise your export: source
                        column names as keys, canonical fields as values.
                      </span>
                    </label>
                    <button className="primary" disabled={busy || !file || !projectId} type="submit">
                      <FileUp aria-hidden="true" size={16} />
                      {busy ? "Importing…" : "Upload and import"}
                    </button>
                  </form>
                  {result && (
                    <div className="result-panel">
                      <h3>Import summary</h3>
                      <div className="result-stats">
                        <div className="result-stat">
                          <b>{result.preview?.rows_total ?? 0}</b>
                          <span>Rows read</span>
                        </div>
                        <div className="result-stat">
                          <b>{result.preview?.rows_valid ?? 0}</b>
                          <span>Valid rows</span>
                        </div>
                        <div className="result-stat">
                          <b>{result.preview?.row_errors?.length ?? 0}</b>
                          <span>Row errors</span>
                        </div>
                        <div className="result-stat">
                          <b>{result.upserted ?? 0}</b>
                          <span>New or updated</span>
                        </div>
                      </div>
                      <ConfirmedNotice
                        ids={result.already_confirmed_ids ?? []}
                        message="They are already in the knowledge base and will not be analyzed again."
                      />
                      {result.import_batch_id && (result.upserted ?? 0) > 0 && (
                        <button
                          className="primary"
                          onClick={() => {
                            setAnalysisBatch(result.import_batch_id ?? null);
                            setScreen("analysis");
                          }}
                          type="button"
                        >
                          <Sparkles aria-hidden="true" size={16} />
                          Analyze these defects
                        </button>
                      )}
                      <details>
                        <summary>Full import report</summary>
                        <pre>{JSON.stringify(result, null, 2)}</pre>
                      </details>
                    </div>
                  )}
                </div>
                <div className="import-col">
                  <AdoPanel
                    csrfToken={csrfToken}
                    isAdmin={selectedProject?.role === "ADMIN"}
                    onOpenAnalysis={(batchId) => {
                      setAnalysisBatch(batchId ?? null);
                      setScreen("analysis");
                    }}
                    projectId={projectId}
                  />
                </div>
              </div>
            ) : (
              <p className="notice">A QA Lead or Admin role is required to import or sync defects.</p>
            )
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
                        <th>State</th>
                        <th title="The AI proposal, or the reviewed RCA once approved or corrected">AI root cause</th>
                        <th>ADO root cause</th>
                        <th>SDLC phase</th>
                        <th>Confidence</th>
                        <th>RCA status</th>
                      </tr>
                    </thead>
                    <tbody>
                      {defects.map((defect) => (
                        <tr
                          className={selectedDefect?.ado_id === defect.ado_id ? "clickable-row selected-row" : "clickable-row"}
                          key={defect.ado_id}
                          onClick={() => void showDefect(defect.ado_id)}
                        >
                          <td className="id-cell">#{defect.ado_id}</td>
                          <td>{defect.title}</td>
                          <td>{defect.module || "—"}</td>
                          <td>
                            <span className="state-pill">{defect.state || "Unknown"}</span>
                          </td>
                          <td>{defect.rca?.root_cause?.label ?? "—"}</td>
                          <td>{defect.ado_root_cause || "—"}</td>
                          <td>{defect.rca?.sdlc_phase?.label ?? "—"}</td>
                          <td className="num-cell">
                            {typeof defect.rca?.confidence === "number" ? `${Math.round(defect.rca.confidence * 100)}%` : "—"}
                          </td>
                          <td>
                            {defect.rca ? (
                              <span className={`rca-pill ${defect.rca.status.toLowerCase()}`}>{rcaStatusText(defect.rca)}</span>
                            ) : (
                              <span className="muted">Not analyzed</span>
                            )}
                          </td>
                        </tr>
                      ))}
                      {defects.length === 0 && (
                        <tr>
                          <td className="empty-cell" colSpan={9}>
                            No defects yet. Import a file or pull from Azure DevOps on the Import page.
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
                        <p className="eyebrow">Defect #{selectedDefect.ado_id}</p>
                        <h2>{selectedDefect.title}</h2>
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
                    {selectedDefect.rca && (
                      <div className="rca-summary">
                        <span className={`rca-pill ${selectedDefect.rca.status.toLowerCase()}`}>
                          {rcaStatusText(selectedDefect.rca)}
                        </span>
                        <dl>
                          <dt>AI root cause</dt>
                          <dd>{selectedDefect.rca.root_cause?.label ?? "—"}</dd>
                          <dt>ADO root cause</dt>
                          <dd>{selectedDefect.ado_root_cause || "—"}</dd>
                          <dt>SDLC phase</dt>
                          <dd>{selectedDefect.rca.sdlc_phase?.label ?? "—"}</dd>
                          <dt>AI confidence</dt>
                          <dd>
                            {typeof selectedDefect.rca.confidence === "number"
                              ? `${Math.round(selectedDefect.rca.confidence * 100)}%`
                              : "—"}
                          </dd>
                        </dl>
                      </div>
                    )}
                    <DetailField
                      label="Module / release"
                      value={`${selectedDefect.module || "—"} · ${selectedDefect.release || "—"}`}
                    />
                    <DetailField label="Description" value={selectedDefect.description} />
                    <DetailField label="Reproduction steps" value={selectedDefect.repro_steps} />
                    <DetailField
                      label="Severity / priority"
                      value={`${selectedDefect.severity || "—"} / ${selectedDefect.priority || "—"}`}
                    />
                    <DetailField label="Tags" value={(selectedDefect.tags ?? []).join(", ")} />
                    {selectedRca?.proposed?.rationale && (
                      <DetailField
                        label="AI rationale"
                        value={
                          selectedRca.proposed.rationale +
                          (selectedRca.proposed.evidence_fields?.length
                            ? `
Based on: ${evidenceText(selectedRca.proposed.evidence_fields)}`
                            : "")
                        }
                      />
                    )}
                    {selectedRca && !selectedRca.proposed && (
                      <DetailField
                        label="AI root cause"
                        value={`Needs manual RCA (${selectedRca.failure_reason ?? "unknown"})`}
                      />
                    )}
                    {!selectedRca && <DetailField label="AI root cause" value="Not analyzed yet" />}
                  </aside>
                )}
              </div>
            </section>
          )}
        </main>
      </div>
    </div>
  );
}

function DetailField({ label, value }: { label: string; value: string }) {
  return (
    <div className="detail-field">
      <h3>{label}</h3>
      <p className="preserve-lines">{value || "—"}</p>
    </div>
  );
}

createRoot(document.getElementById("root")!).render(<App />);
