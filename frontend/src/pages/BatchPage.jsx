import { useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { api, ApiError } from "../api";
import DiffBlock from "../components/DiffBlock";
import ErrorBanner from "../components/ErrorBanner";
import Stepper from "../components/Stepper";

const ACTIVE = new Set(["queued", "running"]);
const STATUS_LABEL = {
  queued: "Queued",
  running: "Running",
  ready: "Ready for review",
  pr_open: "Pull request open",
  no_changes: "No changes",
  failed: "Failed",
};

export default function BatchPage() {
  const { batchId } = useParams();
  const [batch, setBatch] = useState(null);
  const [scm, setScm] = useState(null);
  const [error, setError] = useState(null);
  const [publishing, setPublishing] = useState(false);

  useEffect(() => {
    let timer;
    let cancelled = false;
    const load = async () => {
      try {
        const data = await api.getBatch(batchId);
        if (cancelled) return;
        setBatch(data);
        if (ACTIVE.has(data.status)) timer = setTimeout(load, 2000);
        else api.scmStatus(data.project_key).then(setScm).catch(() => {});
      } catch (err) {
        if (!cancelled) setError(err instanceof ApiError ? err.message : String(err));
      }
    };
    load();
    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
  }, [batchId]);

  const publish = async () => {
    if (!window.confirm(`Push ${batch.branch} and open a pull request into ${batch.base_branch}?`)) return;
    setError(null);
    setPublishing(true);
    try {
      setBatch(await api.createPullRequest(batchId));
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err));
    } finally {
      setPublishing(false);
    }
  };

  if (!batch) {
    return (
      <div className="page">
        <ErrorBanner message={error} onDismiss={() => setError(null)} />
        {!error && <div className="empty">Loading…</div>}
      </div>
    );
  }

  const stage = batch.status === "pr_open" ? 4 : batch.status === "ready" ? 2 : 1;
  const steps = batch.steps || [];

  return (
    <div className="page">
      <div className="page-head">
        <Link className="link-back" to={`/projects/${batch.project_key}/plan`}>
          ← Remediation plan
        </Link>
        <div className="title-row">
          <h1>Run #{batch.id}</h1>
          <span className={`pill pill-${batch.status}`}>{STATUS_LABEL[batch.status] || batch.status}</span>
        </div>
        <Stepper current={stage} />
      </div>

      <ErrorBanner message={error} onDismiss={() => setError(null)} />
      {batch.status === "failed" && <pre className="banner banner-error pre-wrap">{batch.error}</pre>}
      {batch.status === "no_changes" && (
        <div className="banner banner-warn">Nothing changed, so no branch was kept. See the steps below for why.</div>
      )}

      {batch.branch && (
        <p className="caption">
          Branch <code>{batch.branch}</code> from <code>{batch.base_branch}</code>
        </p>
      )}

      <ol className="timeline">
        {steps.map((step, i) => (
          <StepCard key={i} step={step} />
        ))}
        {ACTIVE.has(batch.status) && steps.length === 0 && <li className="step running">Preparing branch…</li>}
      </ol>

      {batch.diff && (
        <section className="card">
          <h2>Changes</h2>
          <DiffBlock diff={batch.diff} />
        </section>
      )}

      {(batch.status === "ready" || batch.status === "pr_open") && (
        <section className="card">
          <div className="card-head">
            <h2>Pull request</h2>
            {scm?.provider && <span className="chip">{scm.provider} · {scm.repository}</span>}
          </div>
          {batch.status === "pr_open" ? (
            <div className="banner banner-success">
              Pull request opened —{" "}
              <a href={batch.pr_url} target="_blank" rel="noreferrer">
                {batch.pr_url}
              </a>
            </div>
          ) : (
            <>
              <p className="pr-title">{batch.pr_title}</p>
              <pre className="code-block compact pr-body">{batch.pr_description}</pre>
              {scm && !scm.ready && (
                <div className="banner banner-warn">
                  {scm.reason} You can still push by hand: <code>git push -u origin {batch.branch}</code>
                </div>
              )}
              <div className="button-row">
                <button className="btn btn-primary" onClick={publish} disabled={publishing || !scm?.ready}>
                  {publishing ? "Pushing…" : "Create Bitbucket PR"}
                </button>
              </div>
            </>
          )}
        </section>
      )}
    </div>
  );
}

function StepCard({ step }) {
  const icon = { running: "…", done: "✓", no_change: "–", failed: "!" }[step.status] || "•";
  return (
    <li className={`step ${step.status}`}>
      <div className="step-head">
        <span className="step-icon">{icon}</span>
        <div className="step-title">
          <strong>{step.kind === "mechanical" ? "Mechanical recipes" : step.title}</strong>
          <span className="caption">
            {step.kind === "mechanical" ? "0 tokens" : `AI session · ${step.rule}`}
            {step.commit && <> · commit <code>{step.commit}</code></>}
          </span>
        </div>
      </div>

      {step.kind === "mechanical" && step.applied?.length > 0 && (
        <ul className="occurrences">
          {step.applied.map((a) => (
            <li key={a.issue_id}>
              <span>{a.file}:{a.line}</span>
              <span className="occ-note">{a.recipe}</span>
            </li>
          ))}
        </ul>
      )}
      {step.failed?.length > 0 && (
        <ul className="occurrences">
          {step.failed.map((f) => (
            <li key={f.issue_id}>
              <span>{f.file}:{f.line}</span>
              <span className="occ-note warn">{f.reason}</span>
            </li>
          ))}
        </ul>
      )}
      {step.summary && <p className="step-summary">{step.summary}</p>}
      {step.error && <p className="step-summary warn">{step.error}</p>}
    </li>
  );
}
