import { useEffect, useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";
import { api, ApiError } from "../api";
import ErrorBanner from "../components/ErrorBanner";

const SEVERITIES = ["BLOCKER", "CRITICAL", "MAJOR", "MINOR", "INFO"];
const SEVERITY_LABEL = { BLOCKER: "Blocker", CRITICAL: "High", MAJOR: "Medium", MINOR: "Low", INFO: "Info" };
const DEP_SEVERITIES = ["critical", "high", "moderate", "low", "unknown"];
const DEP_SEVERITY_LABEL = { critical: "Critical", high: "High", moderate: "Moderate", low: "Low", unknown: "Unknown" };

function StatTile({ value, label, tone }) {
  return (
    <div className={`stat-tile ${tone ? `stat-${tone}` : ""}`}>
      <span className="stat-value">{value}</span>
      <span className="stat-label">{label}</span>
    </div>
  );
}

function MiniBreakdown({ counts, order, labels }) {
  const total = Object.values(counts).reduce((a, b) => a + b, 0);
  if (!total) return <span className="caption">None</span>;
  return (
    <div className="mini-breakdown">
      {order.filter((k) => counts[k]).map((k) => (
        <span key={k} className={`mini-pill mini-${k}`}>
          {counts[k]} {labels[k] || k}
        </span>
      ))}
    </div>
  );
}

export default function DashboardPage() {
  const navigate = useNavigate();
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [showAll, setShowAll] = useState(false);

  useEffect(() => {
    api
      .dashboard()
      .then(setData)
      .catch((err) => setError(err instanceof ApiError ? err.message : String(err)))
      .finally(() => setLoading(false));
  }, []);

  const projects = useMemo(() => {
    if (!data) return [];
    const list = [...data.projects].sort((a, b) => {
      const activity = (p) =>
        p.open_issues + p.fixed_issues + p.vulnerable_dependencies + p.batches_active + p.batches_needs_attention;
      return activity(b) - activity(a);
    });
    return showAll ? list : list.filter((p) => p.total_issues > 0 || p.vulnerable_dependencies > 0);
  }, [data, showAll]);

  const hiddenCount = data ? data.projects.length - projects.length : 0;

  if (loading) return <div className="page"><div className="empty">Loading dashboard…</div></div>;

  return (
    <div className="page">
      <div className="page-head">
        <h1>Dashboard</h1>
        <p className="subtitle">
          Everything being tracked across every project - open issues, fixes in flight, open pull
          requests, and vulnerable dependencies - in one place.
        </p>
      </div>

      <ErrorBanner message={error} onDismiss={() => setError(null)} />

      {data && (
        <>
          <div className="stat-strip">
            <StatTile value={data.totals.open_issues} label="Open issues" />
            <StatTile value={data.totals.fixed_issues} label="Fixed" tone="good" />
            <StatTile value={data.totals.prs_open} label="PRs open" tone="good" />
            <StatTile value={data.totals.vulnerable_dependencies} label="Vulnerable dependencies" tone="warn" />
            <StatTile value={data.totals.batches_active} label="Batches in progress" />
            <StatTile value={data.totals.batches_needs_attention} label="Batches need attention" tone="bad" />
          </div>

          <div className="dash-breakdowns">
            <div className="card">
              <h3>Open issues by severity</h3>
              <div className="sev-strip">
                {SEVERITIES.map((s) => (
                  <div key={s} className={`sev-tile sev-${s}`}>
                    <span className="sev-count">{data.totals.issue_severity[s] || 0}</span>
                    <span className="sev-name">{SEVERITY_LABEL[s]}</span>
                  </div>
                ))}
              </div>
            </div>
            <div className="card">
              <h3>Vulnerable dependencies by severity</h3>
              <div className="sev-strip">
                {DEP_SEVERITIES.map((s) => (
                  <div key={s} className={`sev-tile sev-${s}`}>
                    <span className="sev-count">{data.totals.dependency_severity[s] || 0}</span>
                    <span className="sev-name">{DEP_SEVERITY_LABEL[s]}</span>
                  </div>
                ))}
              </div>
            </div>
          </div>

          <div className="toolbar">
            <label className="checkbox-label">
              <input type="checkbox" checked={showAll} onChange={(e) => setShowAll(e.target.checked)} />
              Show all {data.projects.length} projects (including those with nothing tracked yet)
            </label>
          </div>

          {projects.length === 0 ? (
            <div className="empty">
              Nothing tracked yet. Sync issues or scan dependencies on a project to see it here.
            </div>
          ) : (
            <table className="dep-table">
              <thead>
                <tr>
                  <th>Project</th>
                  <th>Issues</th>
                  <th>Fixed / PRs</th>
                  <th>Dependencies</th>
                  <th>In progress</th>
                  <th>Needs attention</th>
                </tr>
              </thead>
              <tbody>
                {projects.map((p) => (
                  <tr
                    key={p.key}
                    className="dep-row"
                    onClick={() => navigate(`/projects/${p.key}/issues`, { state: { projectName: p.name } })}
                  >
                    <td>
                      <code className="rule">{p.name}</code>
                      {!p.repo_configured && <div className="caption">No repo configured</div>}
                    </td>
                    <td>
                      <MiniBreakdown counts={p.issue_severity} order={SEVERITIES} labels={SEVERITY_LABEL} />
                    </td>
                    <td>
                      {p.fixed_issues} fixed
                      {p.prs_open > 0 && <span className="pr-created-tag"> ✓ {p.prs_open} PR{p.prs_open === 1 ? "" : "s"}</span>}
                    </td>
                    <td>
                      <MiniBreakdown counts={p.dependency_severity} order={DEP_SEVERITIES} labels={DEP_SEVERITY_LABEL} />
                    </td>
                    <td>{p.batches_active > 0 ? p.batches_active : <span className="caption">—</span>}</td>
                    <td>
                      {p.batches_needs_attention > 0 ? (
                        <span className="fix-tag fix-ai">{p.batches_needs_attention} failed</span>
                      ) : (
                        <span className="caption">—</span>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
          {!showAll && hiddenCount > 0 && (
            <p className="caption" style={{ marginTop: 8 }}>
              {hiddenCount} project{hiddenCount === 1 ? "" : "s"} hidden (nothing tracked yet).
            </p>
          )}
        </>
      )}
    </div>
  );
}
