import { useEffect, useMemo, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { api, ApiError } from "../api";
import BeforeAfter from "../components/BeforeAfter";
import DiffBlock from "../components/DiffBlock";
import ErrorBanner from "../components/ErrorBanner";
import SeverityBadge from "../components/SeverityBadge";
import StatTile from "../components/StatTile";
import Stepper from "../components/Stepper";

const SECTIONS = [
  {
    bucket: "mechanical",
    title: "Mechanical fixes",
    tag: "0 tokens",
    blurb: "Deterministic recipes checked against the exact code Sonar flagged. Pre-selected.",
  },
  {
    bucket: "ai",
    title: "Needs AI",
    tag: "approval required",
    blurb: "These need judgement. Nothing runs until you select a group and approve it — one AI session per group, not per issue.",
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
  const [error, setError] = useState(null);
  const [selected, setSelected] = useState(new Set());
  const [open, setOpen] = useState(new Set());
  const [notes, setNotes] = useState("");
  const [approved, setApproved] = useState(false);
  const [running, setRunning] = useState(false);

  useEffect(() => {
    api
      .plan(projectKey)
      .then((data) => {
        setPlan(data);
        setSelected(new Set(data.groups.filter((g) => g.bucket === "mechanical").map((g) => g.id)));
      })
      .catch((err) => setError(err instanceof ApiError ? err.message : String(err)));
    api.listBatches(projectKey).then(setBatches).catch(() => {});
  }, [projectKey]);

  const byBucket = useMemo(() => {
    const out = { mechanical: [], ai: [], skip: [] };
    for (const g of plan?.groups || []) out[g.bucket].push(g);
    return out;
  }, [plan]);

  const picked = useMemo(() => {
    const groups = (plan?.groups || []).filter((g) => selected.has(g.id));
    const mech = groups.filter((g) => g.bucket === "mechanical");
    const ai = groups.filter((g) => g.bucket === "ai");
    return {
      mech,
      ai,
      mechIssues: mech.reduce((n, g) => n + g.unique, 0),
      aiIssues: ai.reduce((n, g) => n + g.unique, 0),
    };
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

  const needsApproval = picked.ai.length > 0 && !approved;
  const nothing = picked.mech.length + picked.ai.length === 0;

  const run = async () => {
    setError(null);
    setRunning(true);
    try {
      const batch = await api.createBatch(projectKey, {
        mechanical: picked.mech.map((g) => g.id),
        ai: picked.ai.map((g) => g.id),
        approve_ai: picked.ai.length > 0 && approved,
        notes,
      });
      navigate(`/batches/${batch.id}`);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err));
      setRunning(false);
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
  return (
    <div className="page with-actionbar">
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

      <ErrorBanner message={error} onDismiss={() => setError(null)} />

      <div className="stats">
        <StatTile label="Open issues" value={s.total} hint={`${s.duplicates} duplicate${s.duplicates === 1 ? "" : "s"}`} />
        <StatTile label="Patterns" value={s.groups} hint="grouped by rule" />
        <StatTile label="Mechanical" value={s.mechanical} hint="0 tokens" tone="green" />
        <StatTile label="Needs AI" value={s.ai} hint={`${s.ai_groups} group${s.ai_groups === 1 ? "" : "s"}`} tone="amber" />
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
            <strong>{picked.mechIssues}</strong> mechanical fix{picked.mechIssues === 1 ? "" : "es"}
            <span className="dot-sep">·</span>
            <strong>{picked.ai.length}</strong> AI session{picked.ai.length === 1 ? "" : "s"}
            {picked.ai.length > 0 && <span className="muted"> ({picked.aiIssues} issues)</span>}
          </div>
          {picked.ai.length > 0 && (
            <div className="actionbar-ai">
              <input
                className="text-input"
                placeholder="Reviewer notes for the AI (optional)"
                value={notes}
                onChange={(e) => setNotes(e.target.value)}
              />
              <label className="approve">
                <input type="checkbox" checked={approved} onChange={(e) => setApproved(e.target.checked)} />
                I approve AI changes for the {picked.ai.length} selected group{picked.ai.length === 1 ? "" : "s"}
              </label>
            </div>
          )}
          <button className="btn btn-primary" disabled={nothing || needsApproval || running} onClick={run}>
            {running ? "Starting…" : "Run selected fixes"}
          </button>
        </div>
      </div>
    </div>
  );
}

function GroupCard({ group, selectable, checked, onToggle, expanded, onExpand }) {
  const title = group.recipe?.title || group.message;
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
          {group.bucket === "mechanical" && group.recipe && (
            <>
              <p className="why">
                <strong>Why it’s safe:</strong> {group.recipe.why_safe}
              </p>
              <BeforeAfter before={group.recipe.before} after={group.recipe.after} />
              <p className="caption">
                Preview on your code — {group.example.file}:{group.example.line}
              </p>
              <DiffBlock diff={group.example.diff} compact />
            </>
          )}
          {group.bucket === "ai" && (
            <>
              <p className="why">{group.message}</p>
              {group.example.snippet && <pre className="code-block compact">{group.example.snippet}</pre>}
            </>
          )}
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
              </li>
            ))}
          </ul>
        </div>
      )}
    </li>
  );
}
