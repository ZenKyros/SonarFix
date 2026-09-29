import { useEffect, useState } from "react";
import { Link, useLocation, useNavigate, useParams } from "react-router-dom";
import { api, ApiError } from "../api";
import SeverityBadge from "../components/SeverityBadge";
import ErrorBanner from "../components/ErrorBanner";

const SHOWN_LIMIT = 200;

export default function IssuesPage() {
  const { projectKey } = useParams();
  const location = useLocation();
  const navigate = useNavigate();
  const projectName = location.state?.projectName || projectKey;

  const [facets, setFacets] = useState({ severities: [], types: [] });
  const [severity, setSeverity] = useState("All");
  const [type, setType] = useState("All");
  const [issues, setIssues] = useState([]);
  const [loading, setLoading] = useState(true);
  const [syncing, setSyncing] = useState(false);
  const [error, setError] = useState(null);

  useEffect(() => {
    api.facets(projectKey).then(setFacets).catch(() => {});
  }, [projectKey]);

  useEffect(() => {
    setLoading(true);
    api
      .listIssues(projectKey, {
        severity: severity === "All" ? undefined : severity,
        type: type === "All" ? undefined : type,
      })
      .then(setIssues)
      .catch((err) => setError(err instanceof ApiError ? err.message : String(err)))
      .finally(() => setLoading(false));
  }, [projectKey, severity, type]);

  const handleSync = async () => {
    setSyncing(true);
    try {
      await api.syncIssues(projectKey);
      setError(null);
      // Reload issues after sync
      const updated = await api.listIssues(projectKey, {
        severity: severity === "All" ? undefined : severity,
        type: type === "All" ? undefined : type,
      });
      setIssues(updated);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err));
    } finally {
      setSyncing(false);
    }
  };

  const shown = issues.slice(0, SHOWN_LIMIT);

  return (
    <div className="page">
      <div className="page-head">
        <button className="link-back" onClick={() => navigate("/")}>
          ← Projects
        </button>
        <h1>{projectName}</h1>
      </div>

      <ErrorBanner message={error} onDismiss={() => setError(null)} />

      <div className="toolbar">
        <select className="select" value={severity} onChange={(e) => setSeverity(e.target.value)}>
          <option>All</option>
          {facets.severities.map((s) => (
            <option key={s}>{s}</option>
          ))}
        </select>
        <select className="select" value={type} onChange={(e) => setType(e.target.value)}>
          <option>All</option>
          {facets.types.map((t) => (
            <option key={t}>{t}</option>
          ))}
        </select>
        <button
          className="btn btn-outline"
          onClick={handleSync}
          disabled={syncing}
        >
          {syncing ? "Syncing…" : "Sync from SonarQube"}
        </button>
        <a
          className="btn btn-outline"
          href={api.reportUrl(projectKey)}
          download
        >
          Download HTML report
        </a>
      </div>

      {loading ? (
        <div className="empty">Loading issues…</div>
      ) : shown.length === 0 ? (
        <div className="empty">No issues match these filters.</div>
      ) : (
        <>
          <p className="caption">{issues.length} issues, worst first.</p>
          <ul className="issue-cards">
            {shown.map((issue) => (
              <li key={issue.id}>
                <Link to={`/issues/${issue.id}`} className="issue-card">
                  <div className="issue-card-head">
                    <SeverityBadge severity={issue.severity} />
                    <code className="rule">{issue.rule}</code>
                    <span className="loc">
                      {issue.file_path}
                      {issue.line ? `:${issue.line}` : ""}
                    </span>
                  </div>
                  <p className="issue-message">{issue.message}</p>
                </Link>
              </li>
            ))}
          </ul>
          {issues.length > SHOWN_LIMIT && (
            <p className="caption">
              Showing the first {SHOWN_LIMIT}. Narrow the filters to see the rest.
            </p>
          )}
        </>
      )}
    </div>
  );
}
