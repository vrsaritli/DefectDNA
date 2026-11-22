import { useCallback, useEffect, useMemo, useState } from "react";
import {
  Bar,
  BarChart,
  CartesianGrid,
  Legend,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
  type TooltipProps,
} from "recharts";
import { api, errorMessage } from "./api";

type Ranked = { key: string; count: number };
type MonthRow = { month: string; total: number; by_root_cause: Record<string, number> };
type Labels = Record<"root_cause" | "sdlc_phase" | "testing_gap", Record<string, string>>;
type Summary = {
  total: number;
  approved: number;
  corrected: number;
  ai_agreement_rate: number | null;
  by_root_cause: Ranked[];
  by_module: Ranked[];
  by_release: Ranked[];
  by_sdlc_phase: Ranked[];
  by_testing_gap: Ranked[];
  by_month: MonthRow[];
  filters: { modules: string[]; releases: string[]; months: string[] };
  labels: Labels;
  note: string;
};
type TrendRow = Record<string, string | number> & {
  total: number;
  by_root_cause: Record<string, number>;
};

// Validated categorical palette (dataviz reference, light mode), fixed order.
const SERIES = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#6250d6"];
const OTHER_COLOR = "#9a9993";
const OTHER_KEY = "__other";
const MAX_SERIES = SERIES.length;
const GROUP_OPTIONS = [
  { value: "module", label: "Module" },
  { value: "release", label: "Release" },
  { value: "month", label: "Month" },
  { value: "testing_gap", label: "Testing gap" },
  { value: "sdlc_phase", label: "SDLC phase" },
];

export function shortPath(value: string): string {
  const parts = value.split("\\").filter(Boolean);
  return parts.length > 2 ? parts.slice(-2).join(" › ") : parts.join(" › ") || value;
}

function percent(value: number | null | undefined): string {
  return value === null || value === undefined ? "—" : `${Math.round(value * 100)}%`;
}

function filenameFrom(header: string | null, fallback: string): string {
  const match = header?.match(/filename="([^"]+)"/);
  return match?.[1] ?? fallback;
}

export function Dashboard({ projectId, canRebuild, csrfToken }: { projectId: string; canRebuild: boolean; csrfToken: string }) {
  const [summary, setSummary] = useState<Summary | null>(null);
  const [rows, setRows] = useState<TrendRow[]>([]);
  const [groupBy, setGroupBy] = useState("module");
  const [module, setModule] = useState("");
  const [release, setRelease] = useState("");
  const [monthFrom, setMonthFrom] = useState("");
  const [monthTo, setMonthTo] = useState("");
  const [exportType, setExportType] = useState("rcas");
  const [exportFormat, setExportFormat] = useState("xlsx");
  const [colorKeys, setColorKeys] = useState<string[] | null>(null);
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  const filterParams = useMemo(() => {
    const params = new URLSearchParams();
    if (module) params.set("module", module);
    if (release) params.set("release", release);
    if (monthFrom) params.set("month_from", monthFrom);
    if (monthTo) params.set("month_to", monthTo);
    return params;
  }, [module, release, monthFrom, monthTo]);

  const load = useCallback(async () => {
    if (!projectId) return;
    setError("");
    const base = `/api/v1/projects/${encodeURIComponent(projectId)}`;
    try {
      const tableParams = new URLSearchParams(filterParams);
      tableParams.set("group_by", groupBy);
      const [nextSummary, table] = await Promise.all([
        api<Summary>(`${base}/trends/summary?${filterParams.toString()}`),
        api<{ rows: TrendRow[] }>(`${base}/trends?${tableParams.toString()}`),
      ]);
      setSummary(nextSummary);
      setRows(table.rows);
    } catch (err) {
      setError(errorMessage(err, "Could not load trends"));
    }
  }, [projectId, filterParams, groupBy]);

  useEffect(() => {
    void load();
  }, [load]);

  // Colors follow the root cause, not its rank: fixed once per project from the
  // unfiltered data, so filtering never repaints the remaining series.
  useEffect(() => {
    setColorKeys(null);
    setModule("");
    setRelease("");
    setMonthFrom("");
    setMonthTo("");
  }, [projectId]);
  useEffect(() => {
    if (colorKeys || !summary || filterParams.toString()) return;
    const order = Object.keys(summary.labels.root_cause);
    const top = summary.by_root_cause.slice(0, MAX_SERIES).map((item) => item.key);
    setColorKeys(order.filter((code) => top.includes(code)).concat(top.filter((code) => !order.includes(code))));
  }, [summary, colorKeys, filterParams]);

  const colorKeyList = colorKeys ?? [];
  const colorOf = (code: string) => {
    const index = colorKeyList.indexOf(code);
    return index >= 0 ? SERIES[index] : OTHER_COLOR;
  };
  const labelOf = (code: string) =>
    code === OTHER_KEY ? "Other" : summary?.labels.root_cause[code] ?? code;

  const monthData = useMemo(() => {
    return (summary?.by_month ?? []).map((row) => {
      const point: Record<string, string | number> = { month: row.month, total: row.total };
      let other = 0;
      for (const [code, count] of Object.entries(row.by_root_cause)) {
        if (colorKeyList.includes(code)) point[code] = count;
        else other += count;
      }
      if (other) point[OTHER_KEY] = other;
      return point;
    });
  }, [summary, colorKeyList]);
  const stackKeys = useMemo(() => {
    const present = new Set(monthData.flatMap((point) => Object.keys(point)));
    const keys = colorKeyList.filter((code) => present.has(code));
    return present.has(OTHER_KEY) ? [...keys, OTHER_KEY] : keys;
  }, [monthData, colorKeyList]);

  async function rebuild() {
    setBusy(true);
    setMessage("");
    setError("");
    try {
      const result = await api<{ facts: number }>(
        `/api/v1/projects/${encodeURIComponent(projectId)}/trends/rebuild`,
        { method: "POST" },
        csrfToken,
      );
      setMessage(`Trends rebuilt from ${result.facts} reviewed RCA(s).`);
      setColorKeys(null);
      await load();
    } catch (err) {
      setError(errorMessage(err, "Rebuild failed"));
    } finally {
      setBusy(false);
    }
  }

  async function download() {
    setBusy(true);
    setError("");
    try {
      const params = new URLSearchParams({ type: exportType, format: exportFormat });
      const response = await fetch(
        `/api/v1/projects/${encodeURIComponent(projectId)}/export?${params.toString()}`,
        { credentials: "include" },
      );
      if (!response.ok) throw new Error(`Export failed (${response.status})`);
      const blob = await response.blob();
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = filenameFrom(
        response.headers.get("Content-Disposition"),
        `defectdna-${exportType}.${exportFormat}`,
      );
      link.click();
      URL.revokeObjectURL(url);
      if (response.headers.get("X-Export-Truncated") === "true") {
        setMessage("Export truncated at the configured row limit.");
      }
    } catch (err) {
      setError(errorMessage(err, "Export failed"));
    } finally {
      setBusy(false);
    }
  }

  const topCause = summary?.by_root_cause[0];
  const topModule = summary?.by_module[0];
  const tableCodes = useMemo(() => {
    const seen = new Set(rows.flatMap((row) => Object.keys(row.by_root_cause)));
    const ordered = Object.keys(summary?.labels.root_cause ?? {}).filter((code) => seen.has(code));
    return ordered.concat([...seen].filter((code) => !ordered.includes(code)));
  }, [rows, summary]);

  const groupLabel = (value: string | number) => {
    const text = String(value);
    if (groupBy === "testing_gap") return summary?.labels.testing_gap[text] ?? text;
    if (groupBy === "sdlc_phase") return summary?.labels.sdlc_phase[text] ?? text;
    if (groupBy === "module" || groupBy === "release") return shortPath(text);
    return text;
  };

  return (
    <section className="dashboard">
      <div className="dash-toolbar">
        <div className="dash-filters" role="group" aria-label="Trend filters">
          <label>
            Module
            <select value={module} onChange={(event) => setModule(event.target.value)}>
              <option value="">All modules</option>
              {summary?.filters.modules.map((value) => (
                <option key={value} value={value}>
                  {shortPath(value)}
                </option>
              ))}
            </select>
          </label>
          <label>
            Release
            <select value={release} onChange={(event) => setRelease(event.target.value)}>
              <option value="">All releases</option>
              {summary?.filters.releases.map((value) => (
                <option key={value} value={value}>
                  {shortPath(value)}
                </option>
              ))}
            </select>
          </label>
          <label>
            From
            <input type="month" value={monthFrom} onChange={(event) => setMonthFrom(event.target.value)} />
          </label>
          <label>
            To
            <input type="month" value={monthTo} onChange={(event) => setMonthTo(event.target.value)} />
          </label>
          {filterParams.toString() && (
            <button
              className="text-button"
              onClick={() => {
                setModule("");
                setRelease("");
                setMonthFrom("");
                setMonthTo("");
              }}
              type="button"
            >
              Clear filters
            </button>
          )}
        </div>
        <div className="dash-actions">
          <select aria-label="Export data" value={exportType} onChange={(event) => setExportType(event.target.value)}>
            <option value="rcas">RCAs</option>
            <option value="trends">Trends</option>
            <option value="defects">Defects</option>
          </select>
          <select aria-label="Export format" value={exportFormat} onChange={(event) => setExportFormat(event.target.value)}>
            <option value="xlsx">Excel (.xlsx)</option>
            <option value="csv">CSV</option>
          </select>
          <button className="primary" disabled={busy} onClick={() => void download()} type="button">
            Export
          </button>
          {canRebuild && (
            <button className="secondary" disabled={busy} onClick={() => void rebuild()} type="button" title="Re-derive trend counts from all reviewed RCAs">
              Rebuild
            </button>
          )}
        </div>
      </div>

      {message && <p className="notice">{message}</p>}
      {error && <p className="notice error">{error}</p>}

      <div className="stat-grid">
        <StatCard
          icon="check"
          title="Reviewed RCAs"
          value={summary ? String(summary.total) : "—"}
          hint={summary ? `${summary.approved} approved · ${summary.corrected} corrected` : "Loading…"}
        />
        <StatCard
          icon="spark"
          title="AI agreement"
          value={percent(summary?.ai_agreement_rate)}
          hint="Share of AI proposals approved unchanged"
        />
        <StatCard
          icon="target"
          title="Top root cause"
          value={topCause ? labelOf(topCause.key) : "—"}
          hint={topCause && summary ? `${topCause.count} of ${summary.total} reviewed RCAs` : "No reviewed RCAs yet"}
        />
        <StatCard
          icon="layers"
          title="Most affected module"
          value={topModule ? shortPath(topModule.key) : "—"}
          hint={topModule ? `${topModule.count} reviewed defect(s)` : "No reviewed RCAs yet"}
        />
      </div>

      {summary && summary.total === 0 ? (
        <div className="card empty-dashboard">
          <h2>No reviewed RCAs yet</h2>
          <p className="muted">
            Trends count approved and corrected RCAs only. Approve or correct AI proposals on the
            AI analysis tab and they will appear here within a few seconds.
          </p>
        </div>
      ) : (
        <>
          <div className="chart-grid">
            <div className="card span-2">
              <div className="card-head">
                <div>
                  <h2>Root causes by closed month</h2>
                  <p className="muted">Reviewed RCAs per month, split by confirmed root cause</p>
                </div>
              </div>
              <div className="chart-box" role="img" aria-label="Stacked bar chart of reviewed RCAs per month by root cause. The breakdown table below lists the same numbers.">
                <ResponsiveContainer width="100%" height={300}>
                  <BarChart data={monthData} margin={{ top: 8, right: 8, bottom: 0, left: -16 }} barCategoryGap="28%">
                    <CartesianGrid stroke="#e1e0d9" strokeDasharray="0" vertical={false} />
                    <XAxis dataKey="month" stroke="#898781" fontSize={12} tickLine={false} axisLine={{ stroke: "#c3c2b7" }} />
                    <YAxis allowDecimals={false} stroke="#898781" fontSize={12} tickLine={false} axisLine={false} />
                    <Tooltip cursor={{ fill: "rgba(11,11,11,0.04)" }} content={<MonthTooltip labelOf={labelOf} />} />
                    <Legend
                      iconType="circle"
                      iconSize={9}
                      wrapperStyle={{ fontSize: 12, color: "#52514e", paddingTop: 8 }}
                      formatter={(value: string) => <span className="legend-text">{labelOf(value)}</span>}
                    />
                    {stackKeys.map((code, index) => (
                      <Bar
                        key={code}
                        dataKey={code}
                        name={code}
                        stackId="causes"
                        fill={code === OTHER_KEY ? OTHER_COLOR : colorOf(code)}
                        stroke="#ffffff"
                        strokeWidth={2}
                        radius={index === stackKeys.length - 1 ? [4, 4, 0, 0] : [0, 0, 0, 0]}
                        maxBarSize={56}
                      />
                    ))}
                  </BarChart>
                </ResponsiveContainer>
              </div>
            </div>
            <div className="card">
              <div className="card-head">
                <div>
                  <h2>Most affected modules</h2>
                  <p className="muted">Reviewed defects per module</p>
                </div>
              </div>
              <RankedBars items={(summary?.by_module ?? []).slice(0, 8)} label={shortPath} total={summary?.total ?? 0} />
            </div>
          </div>

          <div className="chart-grid three">
            <div className="card">
              <div className="card-head">
                <div>
                  <h2>Root cause mix</h2>
                  <p className="muted">Confirmed root causes</p>
                </div>
              </div>
              <RankedBars
                items={summary?.by_root_cause ?? []}
                label={labelOf}
                total={summary?.total ?? 0}
                colorFor={colorOf}
              />
            </div>
            <div className="card">
              <div className="card-head">
                <div>
                  <h2>Testing gaps</h2>
                  <p className="muted">Why testing missed them</p>
                </div>
              </div>
              <RankedBars
                items={summary?.by_testing_gap ?? []}
                label={(code) => summary?.labels.testing_gap[code] ?? code}
                total={summary?.total ?? 0}
              />
            </div>
            <div className="card">
              <div className="card-head">
                <div>
                  <h2>SDLC phase introduced</h2>
                  <p className="muted">Where the defect entered</p>
                </div>
              </div>
              <RankedBars
                items={summary?.by_sdlc_phase ?? []}
                label={(code) => summary?.labels.sdlc_phase[code] ?? code}
                total={summary?.total ?? 0}
              />
            </div>
          </div>

          <div className="card">
            <div className="card-head">
              <div>
                <h2>Breakdown</h2>
                <p className="muted">{summary?.note}</p>
              </div>
              <label className="inline-select">
                Group by
                <select value={groupBy} onChange={(event) => setGroupBy(event.target.value)}>
                  {GROUP_OPTIONS.map((option) => (
                    <option key={option.value} value={option.value}>
                      {option.label}
                    </option>
                  ))}
                </select>
              </label>
            </div>
            <div className="table-wrap">
              <table className="numeric-table">
                <thead>
                  <tr>
                    <th>{GROUP_OPTIONS.find((option) => option.value === groupBy)?.label}</th>
                    <th className="num">Total</th>
                    {tableCodes.map((code) => (
                      <th className="num" key={code}>
                        <span className="swatch" style={{ background: colorOf(code) }} aria-hidden="true" />
                        {labelOf(code)}
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {rows.map((row) => (
                    <tr key={String(row[groupBy])}>
                      <td title={String(row[groupBy])}>{groupLabel(row[groupBy])}</td>
                      <td className="num strong">{row.total}</td>
                      {tableCodes.map((code) => (
                        <td className="num" key={code}>
                          {row.by_root_cause[code] ?? <span className="zero">0</span>}
                        </td>
                      ))}
                    </tr>
                  ))}
                  {rows.length === 0 && (
                    <tr>
                      <td className="empty-cell" colSpan={tableCodes.length + 2}>
                        No reviewed RCAs match these filters.
                      </td>
                    </tr>
                  )}
                </tbody>
              </table>
            </div>
          </div>
        </>
      )}
    </section>
  );
}

function MonthTooltip({ active, payload, label, labelOf }: TooltipProps<number, string> & { labelOf: (code: string) => string }) {
  if (!active || !payload?.length) return null;
  const items = payload.filter((item) => Number(item.value) > 0).reverse();
  const total = items.reduce((sum, item) => sum + Number(item.value), 0);
  return (
    <div className="chart-tooltip">
      <strong>{label}</strong>
      <span className="muted">{total} reviewed RCA(s)</span>
      {items.map((item) => (
        <div className="tooltip-row" key={String(item.dataKey)}>
          <span className="swatch" style={{ background: item.color }} />
          <span>{labelOf(String(item.dataKey))}</span>
          <span className="num">{item.value}</span>
        </div>
      ))}
    </div>
  );
}

function RankedBars({
  items,
  label,
  total,
  colorFor,
}: {
  items: Ranked[];
  label: (key: string) => string;
  total: number;
  colorFor?: (key: string) => string;
}) {
  const max = Math.max(1, ...items.map((item) => item.count));
  if (items.length === 0) return <p className="muted">No data for these filters.</p>;
  return (
    <ul className="ranked-list">
      {items.map((item) => (
        <li key={item.key} title={`${label(item.key)}: ${item.count} (${total ? Math.round((item.count / total) * 100) : 0}%)`}>
          <div className="ranked-head">
            <span className="ranked-label">
              {colorFor && <span className="swatch" style={{ background: colorFor(item.key) }} aria-hidden="true" />}
              {label(item.key)}
            </span>
            <span className="ranked-value">
              {item.count}
              <span className="muted"> · {total ? Math.round((item.count / total) * 100) : 0}%</span>
            </span>
          </div>
          <div className="ranked-track">
            <div
              className="ranked-fill"
              style={{ width: `${(item.count / max) * 100}%`, background: colorFor ? colorFor(item.key) : undefined }}
            />
          </div>
        </li>
      ))}
    </ul>
  );
}

const ICONS: Record<string, string> = {
  check: "M20 6 9 17l-5-5",
  spark: "M12 3v4M12 17v4M3 12h4M17 12h4M5.6 5.6l2.8 2.8M15.6 15.6l2.8 2.8M5.6 18.4l2.8-2.8M15.6 8.4l2.8-2.8",
  target: "M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18ZM12 16a4 4 0 1 0 0-8 4 4 0 0 0 0 8ZM12 12h.01",
  layers: "m12 2 10 5-10 5L2 7l10-5ZM2 17l10 5 10-5M2 12l10 5 10-5",
};

function StatCard({ icon, title, value, hint }: { icon: string; title: string; value: string; hint: string }) {
  return (
    <div className="card stat-card">
      <div className="stat-head">
        <span>{title}</span>
        <svg aria-hidden="true" fill="none" stroke="currentColor" strokeLinecap="round" strokeLinejoin="round" strokeWidth="2" viewBox="0 0 24 24">
          <path d={ICONS[icon]} />
        </svg>
      </div>
      <div className="stat-value" title={value}>
        {value}
      </div>
      <p className="stat-hint">{hint}</p>
    </div>
  );
}
