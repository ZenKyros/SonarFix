import { Fragment, useEffect, useMemo, useState } from "react";
import { useLocation, useNavigate, useParams } from "react-router-dom";
import { api, ApiError } from "../api";
import ErrorBanner from "../components/ErrorBanner";
import { useStatus } from "../status";

const DEP_SEVERITIES = ["critical", "high", "moderate", "low", "unknown", "none"];
const DEP_SEVERITY_LABEL = {
  critical: "Critical",
  high: "High",
  moderate: "Moderate",
  low: "Low",
  unknown: "Unknown",
  none: "Outdated",
};
const DEP_SEVERITY_COLOR = {
  critical: "#8b0000",
  high: "#d64545",
  moderate: "#e08e2b",
  low: "#3b82c4",
  unknown: "#8a8f98",
  none: "#6b7a99",
};

function DepSeverityBadge({ severity }) {
  const color = DEP_SEVERITY_COLOR[severity] || DEP_SEVERITY_COLOR.unknown;
  return (
    <span className="badge" style={{ background: color }}>
      {DEP_SEVERITY_LABEL[severity] || severity}
    </span>
  );
}

const VERDICT_LABEL = {
  safe: "Safe to upgrade",
  caution: "Upgrade with care",
  risky: "Risky - breaks",
  unknown: "Needs investigation",
};

function RecommendationTag({ recommendation, bumpKind, analysis }) {
  if (analysis?.verdict)
    return (
      <span
        className={`verdict-tag verdict-${analysis.verdict}`}
        title={`Verified by a trial upgrade: ${analysis.action}`}
      >
        {VERDICT_LABEL[analysis.verdict]}
      </span>
    );
  if (recommendation === "upgrade")
    return (
      <span
        className="fix-tag fix-auto"
        title={`A ${bumpKind} version bump - safe to apply mechanically, unlikely to change behaviour.`}
      >
        Safe to upgrade
      </span>
    );
  return (
    <span
      className="fix-tag fix-ai"
      title="A major version bump - may include breaking changes. Read the changelog before upgrading."
    >
      Needs review
    </span>
  );
}

function AnalysisPanel({ analysis }) {
  const a = analysis;
  const ran = a.build_status != null;
  return (
    <div className="dep-analysis">
      <div className="dep-verdict">
        <span className={`verdict-tag verdict-${a.verdict}`}>{VERDICT_LABEL[a.verdict]}</span>
        <strong>{a.action}</strong>
        <span className="caption">
          target {a.target_version || "?"} ({a.bump_kind} bump) · analysed in {a.duration_s}s
          {a.analyzed_at ? ` · ${a.analyzed_at}` : ""}
        </span>
      </div>
      <h4>Why</h4>
      <ul>
        {(a.reasons || []).map((r, i) => (
          <li key={i}>{r}</li>
        ))}
      </ul>
      <h4>What happens if you upgrade</h4>
      <ul>
        <li>
          Build:{" "}
          {ran ? <b>{a.build_status}</b> : "not run"}
          {a.test_status ? <> · Tests: <b>{a.test_status}</b></> : " · Tests: none ran"}
        </li>
        <li>
          Code that uses it: {a.usage?.count || 0} source file(s)
          {a.usage?.files?.length ? ` - ${a.usage.files.slice(0, 5).join(", ")}${a.usage.count > 5 ? ", …" : ""}` : ""}
        </li>
        {(a.changed_packages || []).length > 0 && (
          <li>{a.changed_packages.length} package(s) change in the lockfile (see below)</li>
        )}
        {a.changelog && (
          <li>
            Release notes:{" "}
            <a href={a.changelog} target="_blank" rel="noreferrer">
              {a.changelog}
            </a>
          </li>
        )}
      </ul>
      {(a.errors || []).length > 0 && (
        <>
          <h4>Errors after upgrade</h4>
          <pre>{a.errors.join("\n")}</pre>
        </>
      )}
      {a.manifest_diff && (
        <>
          <h4>Manifest change</h4>
          <pre>{a.manifest_diff}</pre>
        </>
      )}
      {(a.changed_packages || []).length > 0 && (
        <>
          <h4>Packages that move with it</h4>
          <pre>{a.changed_packages.slice(0, 60).join("\n")}</pre>
        </>
      )}
    </div>
  );
}

export default function DependenciesPage() {
  const { projectKey } = useParams();
  const location = useLocation();
  const navigate = useNavigate();
  const projectName = location.state?.projectName || projectKey;
  const { track } = useStatus();

  const [deps, setDeps] = useState([]);
  const [scannedAt, setScannedAt] = useState(null);
  const [loading, setLoading] = useState(true);
  const [scanning, setScanning] = useState(false);
  const [error, setError] = useState(null);
  const [severity, setSeverity] = useState("All");
  const [ecosystem, setEcosystem] = useState("All");
  const [query, setQuery] = useState("");
  const [open, setOpen] = useState({});
  const [analyzing, setAnalyzing] = useState(null);
  const [targets, setTargets] = useState({});

  const load = () =>
    api
      .listDependencies(projectKey)
      .then((res) => {
        setDeps(res.dependencies || []);
        setScannedAt(res.scanned_at || null);
      })
      .catch((err) => setError(err instanceof ApiError ? err.message : String(err)))
      .finally(() => setLoading(false));

  useEffect(() => {
    setLoading(true);
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [projectKey]);

  const handleScan = async () => {
    setScanning(true);
    try {
      await track(`Scanning dependencies for ${projectName}…`, async () => {
        const res = await api.scanDependencies(projectKey);
        setDeps(res.dependencies || []);
      });
      setError(null);
      await load();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err));
    } finally {
      setScanning(false);
    }
  };

  const handleAnalyze = async (d) => {
    setAnalyzing(d.id);
    try {
      await track(`Trial-upgrading ${d.package} in a scratch copy…`, () =>
        api.analyzeDependency(d.id, targets[d.id]),
      );
      setError(null);
      await load();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err));
    } finally {
      setAnalyzing(null);
    }
  };

  const counts = useMemo(() => {
    const c = {};
    for (const d of deps) c[d.severity] = (c[d.severity] || 0) + 1;
    return c;
  }, [deps]);

  const ecosystems = useMemo(() => [...new Set(deps.map((d) => d.ecosystem))].sort(), [deps]);

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    return deps
      .filter((d) => severity === "All" || d.severity === severity)
      .filter((d) => ecosystem === "All" || d.ecosystem === ecosystem)
      .filter((d) => !q || d.package.toLowerCase().includes(q));
  }, [deps, severity, ecosystem, query]);

  const filtersActive = severity !== "All" || ecosystem !== "All" || query !== "";

  return (
    <div className="page">
      <div className="page-head">
        <button className="link-back" onClick={() => navigate("/")}>
          ← Projects
        </button>
        <h1>{projectName} · Dependencies</h1>
        <p className="subtitle">
          Every package with a known vulnerability, plus direct dependencies that are behind
          their latest release. Open a row and click "Analyze upgrade" to trial the upgrade in
          a scratch copy of the repo - SonarFix rebuilds and retests there and tells you whether
          it is safe, what changes, and what breaks. Your working tree is never touched.
          {scannedAt && ` Last scanned ${scannedAt}.`}
        </p>
      </div>

      <ErrorBanner message={error} onDismiss={() => setError(null)} />

      <div className="sev-strip">
        <button
          className={`sev-tile ${severity === "All" ? "active" : ""}`}
          onClick={() => setSeverity("All")}
          title="Show every severity"
        >
          <span className="sev-count">{deps.length}</span>
          <span className="sev-name">All</span>
        </button>
        {DEP_SEVERITIES.map((s) => (
          <button
            key={s}
            className={`sev-tile sev-${s} ${severity === s ? "active" : ""}`}
            onClick={() => setSeverity(severity === s ? "All" : s)}
            disabled={!counts[s]}
            title={`${DEP_SEVERITY_LABEL[s]} severity - click to filter, click again to clear`}
          >
            <span className="sev-count">{counts[s] || 0}</span>
            <span className="sev-name">{DEP_SEVERITY_LABEL[s]}</span>
          </button>
        ))}
      </div>

      <div className="toolbar">
        <input
          className="text-input search"
          placeholder="Search package name"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
        />
        <select className="select" value={ecosystem} onChange={(e) => setEcosystem(e.target.value)}>
          <option value="All">All ecosystems</option>
          {ecosystems.map((e) => (
            <option key={e} value={e}>
              {e}
            </option>
          ))}
        </select>
        <div className="toolbar-end">
          <button className="btn btn-primary btn-sm" onClick={handleScan} disabled={scanning}>
            {scanning ? "Scanning…" : "Scan dependencies"}
          </button>
        </div>
      </div>

      {loading ? (
        <div className="empty">Loading dependencies…</div>
      ) : !scannedAt && deps.length === 0 ? (
        <div className="empty">
          Not scanned yet. Click "Scan dependencies" to check every package against known CVEs.
        </div>
      ) : filtered.length === 0 ? (
        <div className="empty">
          No dependencies match these filters.
          {filtersActive && (
            <div>
              <button
                className="link-btn"
                onClick={() => {
                  setSeverity("All");
                  setEcosystem("All");
                  setQuery("");
                }}
              >
                Clear filters
              </button>
            </div>
          )}
        </div>
      ) : (
        <table className="dep-table">
          <thead>
            <tr>
              <th />
              <th>Package</th>
              <th>Ecosystem</th>
              <th>Current → fixed</th>
              <th>Severity</th>
              <th>Advisory</th>
              <th>Recommendation</th>
            </tr>
          </thead>
          <tbody>
            {filtered.map((d, i) => {
              const rowKey = `${d.package}-${d.manifest_path}-${i}`;
              const isOpen = open[rowKey];
              return (
                <Fragment key={rowKey}>
                  <tr
                    className="dep-row"
                    onClick={() => setOpen({ ...open, [rowKey]: !isOpen })}
                  >
                    <td className="dep-chevron">{isOpen ? "▾" : "▸"}</td>
                    <td>
                      <code className="rule">{d.package}</code>
                      {!d.direct && <span className="type-tag"> (transitive)</span>}
                      <div className="caption">{d.manifest_path}</div>
                    </td>
                    <td>{d.ecosystem}</td>
                    <td>
                      {d.current_version} → {d.fixed_version || d.latest_version || "?"}
                      {d.fixed_version && d.latest_version && d.latest_version !== d.fixed_version && (
                        <div className="caption">latest: {d.latest_version}</div>
                      )}
                    </td>
                    <td>
                      <DepSeverityBadge severity={d.severity} />
                    </td>
                    <td className="caption">{d.advisory_id}</td>
                    <td>
                      <RecommendationTag
                        recommendation={d.recommendation}
                        bumpKind={d.bump_kind}
                        analysis={d.analysis}
                      />
                    </td>
                  </tr>
                  {isOpen && (
                    <tr className="dep-detail-row">
                      <td colSpan={7}>
                        <p className="dep-reason">{d.reason || "No further detail available."}</p>
                        {d.analysis && <AnalysisPanel analysis={d.analysis} />}
                        <div className="dep-actions">
                          <button
                            className="btn btn-primary btn-sm"
                            disabled={analyzing !== null}
                            onClick={() => handleAnalyze(d)}
                          >
                            {analyzing === d.id
                              ? "Analyzing…"
                              : d.analysis
                                ? "Re-analyze upgrade"
                                : "Analyze upgrade"}
                          </button>
                          <input
                            className="text-input"
                            placeholder={`target (${d.fixed_version || d.latest_version || "version"})`}
                            value={targets[d.id] || ""}
                            onChange={(e) => setTargets({ ...targets, [d.id]: e.target.value })}
                          />
                        </div>
                      </td>
                    </tr>
                  )}
                </Fragment>
              );
            })}
          </tbody>
        </table>
      )}
    </div>
  );
}
