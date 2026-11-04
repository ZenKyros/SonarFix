import { useEffect, useMemo, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { api, ApiError } from "../api";
import DiffBlock from "../components/DiffBlock";
import ErrorBanner from "../components/ErrorBanner";
import SeverityBadge from "../components/SeverityBadge";
import StatTile from "../components/StatTile";
import Stepper from "../components/Stepper";
import BuildStatusBadge from "../components/BuildStatusBadge";
import TerminalLog from "../components/TerminalLog";
import BuildFailureSummary from "../components/BuildFailureSummary";
import BranchPicker from "../components/BranchPicker";

const SECTIONS = [
  {
    bucket: "ai",
    title: "Issues to fix",
    tag: "AI",
    blurb: "Untick anything you don't want touched, then click Fix with AI. One AI session runs per group of similar issues.",
  },
  {
    bucket: "skip",
    title: "Skipped",
    tag: "not fixed",
    blurb: "Vendored, generated or build-output code. Exclude these paths from the Sonar scan instead of changing them.",
  },
];

export default function PlanPage() {
  const { projectKey } = useParams();
  const navigate = useNavigate();
  const [plan, setPlan] = useState(null);
  const [batches, setBatches] = useState([]);
  const [resolved, setResolved] = useState(null);
  const [tab, setTab] = useState("plan");
  const [error, setError] = useState(null);
  const [selected, setSelected] = useState(new Set());
  const [open, setOpen] = useState(new Set());
  const [notes, setNotes] = useState("");
  const [running, setRunning] = useState(false);

  const loadResolved = () =>
    api
      .resolvedPlan(projectKey)
      .then(setResolved)
      .catch((err) => setError(err instanceof ApiError ? err.message : String(err)));

  useEffect(() => {
    api
      .plan(projectKey)
      .then((data) => {
        setPlan(data);
        setSelected(new Set(data.groups.filter((g) => g.bucket === "ai").map((g) => g.id)));
      })
      .catch((err) => setError(err instanceof ApiError ? err.message : String(err)));
    api.listBatches(projectKey).then(setBatches).catch(() => {});
    loadResolved();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [projectKey]);

  const byBucket = useMemo(() => {
    const out = { ai: [], skip: [] };
    for (const g of plan?.groups || []) out[g.bucket].push(g);
    return out;
  }, [plan]);

  const picked = useMemo(() => {
    const groups = (plan?.groups || []).filter((g) => selected.has(g.id));
    const ai = groups.filter((g) => g.bucket === "ai");
    return { ai, aiIssues: ai.reduce((n, g) => n + g.unique, 0) };
  }, [plan, selected]);

  const toggle = (setter, id) =>
    setter((prev) => {
      const next = new Set(prev);
      next.has(id) ? next.delete(id) : next.add(id);
      return next;
    });

  const selectAll = (bucket, on) =>
    setSelected((prev) => {
      const next = new Set(prev);
      for (const g of byBucket[bucket]) on ? next.add(g.id) : next.delete(g.id);
      return next;
    });

  const nothing = picked.ai.length === 0;

  const run = async () => {
    setError(null);
    setRunning(true);
    try {
      const batch = await api.createBatch(projectKey, {
        mechanical: [],
        ai: picked.ai.map((g) => g.id),
        approve_ai: true, // clicking "Fix with AI" is the approval
        notes,
      });
      navigate(`/batches/${batch.id}`);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err));
      setRunning(false);
    }
  };

  // A different SonarQube branch means different issues: re-sync, then re-plan.
  const onBranchChanged = async () => {
    setError(null);
    try {
      await api.syncIssues(projectKey);
      const data = await api.plan(projectKey);
      setPlan(data);
      setSelected(new Set(data.groups.filter((g) => g.bucket === "ai").map((g) => g.id)));
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err));
    }
  };

  if (!plan) {
    return (
      <div className="page">
        <ErrorBanner message={error} onDismiss={() => setError(null)} />
        {!error && <div className="empty">Building the remediation plan…</div>}
      </div>
    );
  }

  const s = plan.summary;
  const resolvedCount = resolved?.batches?.length || 0;
  return (
    <div className={`page ${tab === "plan" ? "with-actionbar" : ""}`}>
      <div className="page-head">
        <button className="link-back" onClick={() => navigate("/")}>← Projects</button>
        <div className="title-row">
          <h1>{plan.project.name}</h1>
          <Link className="btn btn-ghost" to={`/projects/${projectKey}/issues`} state={{ projectName: plan.project.name }}>
            All issues
          </Link>
        </div>
        <Stepper current={0} />
      </div>

      <BranchPicker projectKey={projectKey} onChanged={onBranchChanged} onError={setError} />

      <ErrorBanner message={error} onDismiss={() => setError(null)} />

      <div className="page-tabs">
        <button className={`page-tab ${tab === "plan" ? "active" : ""}`} onClick={() => setTab("plan")}>
          Plan <span className="count-pill">{s.total}</span>
        </button>
        <button className={`page-tab ${tab === "pr" ? "active" : ""}`} onClick={() => { setTab("pr"); loadResolved(); }}>
          ✓ PR Created <span className="count-pill">{resolvedCount}</span>
        </button>
      </div>

      {tab === "pr" ? (
        <ResolvedBatches resolved={resolved} />
      ) : (
        <>
      <div className="stats">
        <StatTile label="Open issues" value={s.total} hint={`${s.duplicates} duplicate${s.duplicates === 1 ? "" : "s"}`} />
        <StatTile label="Patterns" value={s.groups} hint="grouped by rule" />
        <StatTile label="To fix" value={s.ai} hint={`${s.ai_groups} group${s.ai_groups === 1 ? "" : "s"}`} tone="amber" />
        <StatTile label="Skipped" value={s.skip} hint="vendored / generated" tone="muted" />
      </div>

      {batches.length > 0 && (
        <div className="recent">
          <span className="caption">Recent runs:</span>
          {batches.slice(0, 5).map((b) => (
            <Link key={b.id} className={`pill pill-${b.status}`} to={`/batches/${b.id}`}>
              #{b.id} {b.status.replace("_", " ")}
            </Link>
          ))}
        </div>
      )}

      {SECTIONS.map((section) => {
        const groups = byBucket[section.bucket];
        if (!groups.length) return null;
        const selectable = section.bucket !== "skip";
        const allOn = groups.every((g) => selected.has(g.id));
        return (
          <section key={section.bucket} className={`bucket bucket-${section.bucket}`}>
            <header className="bucket-head">
              <div>
                <h2>
                  {section.title} <span className="bucket-tag">{section.tag}</span>
                </h2>
                <p className="bucket-blurb">{section.blurb}</p>
              </div>
              {selectable && (
                <button className="btn btn-ghost btn-sm" onClick={() => selectAll(section.bucket, !allOn)}>
                  {allOn ? "Clear" : "Select all"}
                </button>
              )}
            </header>
            <ul className="group-list">
              {groups.map((g) => (
                <GroupCard
                  key={g.id}
                  group={g}
                  selectable={selectable}
                  checked={selected.has(g.id)}
                  onToggle={() => toggle(setSelected, g.id)}
                  expanded={open.has(g.id)}
                  onExpand={() => toggle(setOpen, g.id)}
                />
              ))}
            </ul>
          </section>
        );
      })}

      <div className="actionbar">
        <div className="actionbar-inner">
          <div className="actionbar-summary">
            <strong>{picked.aiIssues}</strong> issue{picked.aiIssues === 1 ? "" : "s"} selected
            <span className="muted"> in {picked.ai.length} group{picked.ai.length === 1 ? "" : "s"}</span>
          </div>
          {picked.ai.length > 0 && (
            <div className="actionbar-ai">
              <input
                className="text-input"
                placeholder="Notes for the AI (optional)"
                value={notes}
                onChange={(e) => setNotes(e.target.value)}
              />
            </div>
          )}
          <button className="btn btn-primary" disabled={nothing || running} onClick={run}>
            {running ? "Starting…" : "Fix with AI"}
          </button>
        </div>
      </div>
        </>
      )}
    </div>
  );
}

function GroupCard({ group, selectable, checked, onToggle, expanded, onExpand }) {
  const title = group.message;
  return (
    <li className={`group ${checked ? "is-selected" : ""}`}>
      <div className="group-row">
        {selectable && (
          <input type="checkbox" className="group-check" checked={checked} onChange={onToggle} aria-label={`Select ${group.rule}`} />
        )}
        <button className="group-main" onClick={onExpand} aria-expanded={expanded}>
          <SeverityBadge severity={group.severity} />
          <span className="group-title">
            <span>{title}</span>
            <code className="rule">{group.rule}</code>
          </span>
          <span className="chips">
            <span className="chip">{group.unique} issue{group.unique === 1 ? "" : "s"}</span>
            <span className="chip">{group.files} file{group.files === 1 ? "" : "s"}</span>
            {group.duplicates > 0 && <span className="chip chip-dup">+{group.duplicates} duplicate</span>}
          </span>
          <span className="chevron">{expanded ? "▾" : "▸"}</span>
        </button>
      </div>

      {expanded && (
        <div className="group-body">
          {group.bucket === "ai" && <p className="why">{group.message}</p>}
          {group.bucket === "skip" && <p className="why">{group.skip_reason}</p>}

          <ul className="occurrences">
            {group.occurrences.map((o) => (
              <li key={o.issue_id}>
                <Link to={`/issues/${o.issue_id}`}>
                  {o.file}
                  {o.line ? `:${o.line}` : ""}
                </Link>
                {o.duplicate_of && <span className="chip chip-dup">duplicate</span>}
                {o.note && <span className="occ-note">{o.note}</span>}
                {(o.diff || o.snippet) && (
                  <details className="occ-change" open={group.occurrences.length <= 10}>
                    <summary>{o.diff ? "Before / after" : "Flagged code"}</summary>
                    {o.diff ? (
                      <DiffBlock diff={o.diff} compact />
                    ) : (
                      <pre className="code-block compact">{o.snippet}</pre>
                    )}
                  </details>
                )}
              </li>
            ))}
          </ul>
        </div>
      )}
    </li>
  );
}

const CONFIDENCE_LABEL = { high: "High confidence", medium: "Medium confidence", low: "Low confidence" };

function ResolvedBatches({ resolved }) {
  if (!resolved) return <div className="empty">Loading…</div>;
  if (resolved.batches.length === 0) {
    return (
      <div className="empty">
        Nothing here yet. Once a batch's build (and tests) pass, it moves here - whether or
        not a pull request has been opened for it yet.
      </div>
    );
  }
  return (
    <ul className="group-list resolved-list">
      {resolved.batches.map((batch) => (
        <ResolvedBatchCard key={batch.id} batch={batch} />
      ))}
    </ul>
  );
}

function ResolvedBatchCard({ batch }) {
  const [open, setOpen] = useState(false);
  const issueCount = (batch.steps || []).reduce(
    (n, s) => n + (s.kind === "mechanical" ? (s.applied || []).length : (s.occurrences || []).length),
    0
  );
  const confidence = batch.confidence || {};

  return (
    <li className="group is-selected resolved-batch">
      <button className="group-main" onClick={() => setOpen((v) => !v)} aria-expanded={open}>
        <span className={`confidence-dot confidence-${confidence.level}`} title={confidence.reason} />
        <span className="group-title">
          <span>{batch.pr_title || `Batch #${batch.id}`}</span>
          <code className="rule">{batch.branch}</code>
        </span>
        <span className="chips">
          <span className="chip">{issueCount} issue{issueCount === 1 ? "" : "s"}</span>
          {batch.status === "pr_open" ? (
            <span className="chip chip-pr">✓ PR created</span>
          ) : (
            <span className="chip">✓ Fix applied</span>
          )}
          <span className="chip">{CONFIDENCE_LABEL[confidence.level] || "Confidence unknown"}</span>
        </span>
        <span className="chevron">{open ? "▾" : "▸"}</span>
      </button>

      {open && (
        <div className="group-body resolved-body">
          <div className="button-row">
            <a className="btn btn-outline btn-sm" href={api.batchReportUrl(batch.id)} download>
              HTML report for this PR
            </a>
          </div>

          <div className="status-badge-row">
            <BuildStatusBadge status={batch.build_status} kind="Build" />
            <BuildStatusBadge status={batch.test_status} kind="Test" />
          </div>

          <p className="why">
            <strong>Why this should work ({CONFIDENCE_LABEL[confidence.level]}):</strong> {confidence.reason}
          </p>

          {batch.status === "pr_open" ? (
            <p className="caption">
              Pull request:{" "}
              <a href={batch.pr_url} target="_blank" rel="noreferrer">{batch.pr_url}</a>
            </p>
          ) : (
            <p className="caption">
              Fix committed on <code>{batch.branch}</code>, no pull request yet -{" "}
              <Link to={`/batches/${batch.id}`}>open it to publish</Link>.
            </p>
          )}

          {(batch.steps || []).map((step, i) => (
            <div key={i} className="resolved-step">
              <strong>{step.kind === "mechanical" ? "Mechanical recipes" : step.title}</strong>
              {(step.summary || step.detail) && <p className="why">{step.summary || step.detail}</p>}
              {step.testing && (
                <p className="caption"><strong>How to verify:</strong> {step.testing}</p>
              )}
              <ul className="occurrences">
                {(step.kind === "mechanical" ? step.applied : step.occurrences || []).map((o) => (
                  <li key={o.issue_id}>
                    <Link to={`/issues/${o.issue_id}`}>{o.file}{o.line ? `:${o.line}` : ""}</Link>
                    {o.message && <span className="occ-note">{o.message}</span>}
                  </li>
                ))}
              </ul>
            </div>
          ))}

          {batch.artifacts?.length > 0 && (
            <div className="artifacts">
              <strong>Affected build artifacts</strong>
              <p className="caption">What ships from this change, and exactly which files changed to produce it.</p>
              <ul className="artifact-list">
                {batch.artifacts.map((a, i) => (
                  <li key={i}>
                    <code>{a.file}</code>
                    <span className="caption"> ({a.kind})</span>
                    {a.files?.length > 0 && (
                      <ul className="artifact-files">
                        {a.files.map((f, j) => <li key={j}>{f}</li>)}
                      </ul>
                    )}
                  </li>
                ))}
              </ul>
            </div>
          )}

          <BuildFailureSummary summary={batch.build_summary} />
          <TerminalLog title="Build log" output={batch.build_output} />
          <TerminalLog title="Test log" output={batch.test_output} />

          {batch.diff && (
            <>
              <strong>Before &rarr; after (unified diff)</strong>
              <DiffBlock diff={batch.diff} />
            </>
          )}
        </div>
      )}
    </li>
  );
}
