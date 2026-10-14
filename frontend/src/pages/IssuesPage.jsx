import { useEffect, useMemo, useState } from "react";
import { Link, useLocation, useNavigate, useParams } from "react-router-dom";
import { api, ApiError } from "../api";
import SeverityBadge, { SEVERITY_LABEL } from "../components/SeverityBadge";
import ErrorBanner from "../components/ErrorBanner";
import { useStatus } from "../status";

const SEVERITIES = ["BLOCKER", "CRITICAL", "MAJOR", "MINOR", "INFO"];
const SEV_RANK = Object.fromEntries(SEVERITIES.map((s, i) => [s, i]));
const TYPE_LABEL = {
  BUG: "Bug",
  VULNERABILITY: "Vulnerability",
  CODE_SMELL: "Code smell",
  SECURITY_HOTSPOT: "Hotspot",
};
const SEVERITY_HINT = {
  BLOCKER: "Must fix - breaks the build or a release gate",
  CRITICAL: "High - likely to cause a bug or security issue in production",
  MAJOR: "Medium - a real quality problem worth fixing soon",
  MINOR: "Low impact - fix when convenient",
  INFO: "Informational, no action required",
};
const sevRank = (s) => SEV_RANK[s] ?? SEVERITIES.length;

// SonarQube's own quality-model categories, derived from the issue type -
// the same grouping Sonar's own UI uses (Security / Reliability / Maintainability).
const CATEGORIES = ["SECURITY", "RELIABILITY", "MAINTAINABILITY"];
const CATEGORY_LABEL = {
  SECURITY: "Security",
  RELIABILITY: "Reliability",
  MAINTAINABILITY: "Maintainability",
};
const CATEGORY_HINT = {
  SECURITY: "Vulnerabilities and security hotspots - exploitable weaknesses",
  RELIABILITY: "Bugs - code that is demonstrably wrong",
  MAINTAINABILITY: "Code smells - harder to read, extend or safely change",
};
const TYPE_TO_CATEGORY = {
  VULNERABILITY: "SECURITY",
  SECURITY_HOTSPOT: "SECURITY",
  BUG: "RELIABILITY",
  CODE_SMELL: "MAINTAINABILITY",
};
const categoryOf = (type) => TYPE_TO_CATEGORY[type] || "MAINTAINABILITY";

export default function IssuesPage() {
  const { projectKey } = useParams();
  const location = useLocation();
  const navigate = useNavigate();
  const projectName = location.state?.projectName || projectKey;
  const { track } = useStatus();

  const [issues, setIssues] = useState([]);
  const [fixKind, setFixKind] = useState({});
  const [loading, setLoading] = useState(true);
  const [syncing, setSyncing] = useState(false);
  const [error, setError] = useState(null);

  const [severity, setSeverity] = useState("All");
  const [category, setCategory] = useState("All");
  const [type, setType] = useState("All");
  const [query, setQuery] = useState("");
  const [view, setView] = useState("rules");
  const [open, setOpen] = useState({});
  const [tab, setTab] = useState("open");

  const load = () =>
    api
      .listIssues(projectKey)
      .then(setIssues)
      .catch((err) => setError(err instanceof ApiError ? err.message : String(err)))
      .finally(() => setLoading(false));

  useEffect(() => {
    setLoading(true);
    load();
    // Fix-type tags are a nicety: a slow or failed plan must never block the list.
    api
      .plan(projectKey)
      .then((plan) => {
        const kinds = {};
        for (const g of plan.groups || []) kinds[g.rule] = g.bucket;
        setFixKind(kinds);
      })
      .catch(() => {});
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [projectKey]);

  const handleSync = async () => {
    setSyncing(true);
    try {
      await track(`Syncing issues for ${projectName}…`, async () => {
        await api.syncIssues(projectKey);
      });
      setError(null);
      await load();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err));
    } finally {
      setSyncing(false);
    }
  };

  const fixedCount = useMemo(() => issues.filter((i) => i.fix_status).length, [issues]);
  const openCount = issues.length - fixedCount;

  const tabIssues = useMemo(
    () => issues.filter((i) => (tab === "fixed" ? i.fix_status : !i.fix_status)),
    [issues, tab]
  );

  const counts = useMemo(() => {
    const c = {};
    for (const i of tabIssues) c[i.severity] = (c[i.severity] || 0) + 1;
    return c;
  }, [tabIssues]);

  const categoryCounts = useMemo(() => {
    const c = {};
    for (const i of tabIssues) {
      const cat = categoryOf(i.type);
      c[cat] = (c[cat] || 0) + 1;
    }
    return c;
  }, [tabIssues]);

  const types = useMemo(() => [...new Set(tabIssues.map((i) => i.type))].sort(), [tabIssues]);

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    return tabIssues
      .filter((i) => severity === "All" || i.severity === severity)
      .filter((i) => category === "All" || categoryOf(i.type) === category)
      .filter((i) => type === "All" || i.type === type)
      .filter(
        (i) =>
          !q ||
          i.message.toLowerCase().includes(q) ||
          i.rule.toLowerCase().includes(q) ||
          (i.file_path || "").toLowerCase().includes(q)
      )
      .sort((a, b) => sevRank(a.severity) - sevRank(b.severity));
  }, [tabIssues, severity, category, type, query]);

  const groups = useMemo(() => {
    const byRule = new Map();
    for (const i of filtered) {
      if (!byRule.has(i.rule)) byRule.set(i.rule, []);
      byRule.get(i.rule).push(i);
    }
    return [...byRule.entries()]
      .map(([rule, items]) => ({ rule, items, worst: items[0].severity, type: items[0].type }))
      .sort((a, b) => sevRank(a.worst) - sevRank(b.worst) || b.items.length - a.items.length);
  }, [filtered]);

  const filtersActive = severity !== "All" || category !== "All" || type !== "All" || query !== "";

  return (
    <div className="page">
      <div className="page-head">
        <button className="link-back" onClick={() => navigate("/")}>
          ← Projects
        </button>
        <h1>{projectName}</h1>
        <p className="subtitle">
          {issues.length} open issue{issues.length === 1 ? "" : "s"} in {new Set(issues.map((i) => i.rule)).size} rule
          {new Set(issues.map((i) => i.rule)).size === 1 ? "" : "s"}. Start with the worst, or with a rule that
          has an auto-fix.
        </p>
      </div>

      <ErrorBanner message={error} onDismiss={() => setError(null)} />

      <div className="page-tabs">
        <button className={`page-tab ${tab === "open" ? "active" : ""}`} onClick={() => setTab("open")}>
          Open <span className="count-pill">{openCount}</span>
        </button>
        <button className={`page-tab ${tab === "fixed" ? "active" : ""}`} onClick={() => setTab("fixed")}>
          ✓ Fixed <span className="count-pill">{fixedCount}</span>
        </button>
      </div>

      <div className="sev-strip category-strip">
        <button
          className={`sev-tile ${category === "All" ? "active" : ""}`}
          onClick={() => setCategory("All")}
          title="Show every category"
        >
          <span className="sev-count">{tabIssues.length}</span>
          <span className="sev-name">All</span>
        </button>
        {CATEGORIES.map((c) => (
          <button
            key={c}
            className={`sev-tile cat-${c} ${category === c ? "active" : ""}`}
            onClick={() => setCategory(category === c ? "All" : c)}
            disabled={!categoryCounts[c]}
            title={CATEGORY_HINT[c]}
          >
            <span className="sev-count">{categoryCounts[c] || 0}</span>
            <span className="sev-name">{CATEGORY_LABEL[c]}</span>
          </button>
        ))}
      </div>

      <div className="sev-strip">
        <button
          className={`sev-tile ${severity === "All" ? "active" : ""}`}
          onClick={() => setSeverity("All")}
          title="Show every severity"
        >
          <span className="sev-count">{issues.length}</span>
          <span className="sev-name">All</span>
        </button>
        {SEVERITIES.map((s) => (
          <button
            key={s}
            className={`sev-tile sev-${s} ${severity === s ? "active" : ""}`}
            onClick={() => setSeverity(severity === s ? "All" : s)}
            disabled={!counts[s]}
            title={`${SEVERITY_HINT[s]} - click to filter, click again to clear`}
          >
            <span className="sev-count">{counts[s] || 0}</span>
            <span className="sev-name">{SEVERITY_LABEL[s] || s}</span>
          </button>
        ))}
      </div>

      <div className="toolbar">
        <input
          className="text-input search"
          placeholder="Search message, rule or file"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
        />
        <select className="select" value={type} onChange={(e) => setType(e.target.value)}>
          <option value="All">All types</option>
          {types.map((t) => (
            <option key={t} value={t}>
              {TYPE_LABEL[t] || t}
            </option>
          ))}
        </select>
        <div className="seg">
          <button className={view === "rules" ? "on" : ""} onClick={() => setView("rules")}>
            By rule
          </button>
          <button className={view === "flat" ? "on" : ""} onClick={() => setView("flat")}>
            All issues
          </button>
        </div>
        <div className="toolbar-end">
          <button className="btn btn-outline btn-sm" onClick={handleSync} disabled={syncing}>
            {syncing ? "Syncing…" : "Sync from SonarQube"}
          </button>
          <a className="btn btn-outline btn-sm" href={api.reportUrl(projectKey)} download>
            HTML report
          </a>
        </div>
      </div>

      {loading ? (
        <div className="empty">Loading issues…</div>
      ) : filtered.length === 0 ? (
        <div className="empty">
          No issues match these filters.
          {filtersActive && (
            <div>
              <button
                className="link-btn"
                onClick={() => {
                  setSeverity("All");
                  setCategory("All");
                  setType("All");
                  setQuery("");
                }}
              >
                Clear filters
              </button>
            </div>
          )}
        </div>
      ) : view === "rules" ? (
        <ul className="rule-groups">
          {groups.map((g) => {
            const isOpen = open[g.rule] ?? groups.length === 1;
            return (
              <li key={g.rule} className={`rule-group ${isOpen ? "is-open" : ""}`}>
                <button
                  className="rule-group-head"
                  onClick={() => setOpen({ ...open, [g.rule]: !isOpen })}
                >
                  <span className="chevron">{isOpen ? "▾" : "▸"}</span>
                  <SeverityBadge severity={g.worst} />
                  <span className="rule-group-title">
                    <code className="rule">{g.rule}</code>
                    <span className="rule-sample">{g.items[0].message}</span>
                  </span>
                  <FixTag kind={fixKind[g.rule]} />
                  <span className="type-tag">{TYPE_LABEL[g.type] || g.type}</span>
                  <span className="count-pill">{g.items.length}</span>
                </button>
                {isOpen && (
                  <ul className="rule-group-body">
                    {g.items.map((issue) => (
                      <IssueRow key={issue.id} issue={issue} />
                    ))}
                  </ul>
                )}
              </li>
            );
          })}
        </ul>
      ) : (
        <ul className="rule-group-body flat">
          {filtered.map((issue) => (
            <IssueRow key={issue.id} issue={issue} showRule fixKind={fixKind[issue.rule]} />
          ))}
        </ul>
      )}
    </div>
  );
}

function FixTag({ kind }) {
  if (kind === "mechanical")
    return (
      <span className="fix-tag fix-auto" title="A deterministic recipe fixes every occurrence instantly - no AI, no cost.">
        Auto-fix
      </span>
    );
  if (kind === "ai")
    return (
      <span className="fix-tag fix-ai" title="No automatic recipe exists - fixing this needs an AI-generated change you review and approve.">
        Needs AI
      </span>
    );
  return null;
}

function IssueRow({ issue, showRule, fixKind }) {
  const file = (issue.file_path || "").split("/").pop();
  return (
    <li>
      <Link to={`/issues/${issue.id}`} className={`issue-row ${issue.fix_status ? "has-pr" : ""}`}>
        {showRule && <SeverityBadge severity={issue.severity} />}
        <span className="issue-row-main">
          <span className="issue-row-msg">{issue.message}</span>
          {showRule && <code className="rule">{issue.rule}</code>}
        </span>
        {showRule && <FixTag kind={fixKind} />}
        {issue.fix_status === "pr_open" && <span className="pr-created-tag">✓ PR created</span>}
        {issue.fix_status === "applied" && <span className="pr-created-tag">✓ Fix applied</span>}
        <span className="loc" title={issue.file_path}>
          {file}
          {issue.line ? `:${issue.line}` : ""}
        </span>
      </Link>
    </li>
  );
}
