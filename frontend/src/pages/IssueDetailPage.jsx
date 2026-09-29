import { useEffect, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { api, ApiError } from "../api";
import SeverityBadge from "../components/SeverityBadge";
import ConfidenceBar from "../components/ConfidenceBar";
import ErrorBanner from "../components/ErrorBanner";
import DiffBlock from "../components/DiffBlock";

export default function IssueDetailPage() {
  const { issueId } = useParams();
  const navigate = useNavigate();

  const [detail, setDetail] = useState(null);
  const [state, setState] = useState(null);
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(null);
  const [feedback, setFeedback] = useState("");
  const [ruleOpen, setRuleOpen] = useState(false);
  const [displayedThinking, setDisplayedThinking] = useState("");

  const loadAll = async () => {
    const [d, s] = await Promise.all([
      api.getIssue(issueId),
      api.workflowState(issueId),
    ]);
    setDetail(d);
    setState(s);
  };

  useEffect(() => {
    loadAll().catch((err) => setError(err instanceof ApiError ? err.message : String(err)));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [issueId]);

  // Progressive reveal of thinking text for visual effect
  useEffect(() => {
    if (busy !== "analyze" && state?.plan?.explanation && displayedThinking !== state.plan.explanation) {
      // When analysis completes, animate the thinking text
      let index = 0;
      const text = state.plan.explanation;
      const interval = setInterval(() => {
        if (index <= text.length) {
          setDisplayedThinking(text.slice(0, index));
          index += 3; // Reveal 3 chars at a time
        } else {
          clearInterval(interval);
        }
      }, 10);
      return () => clearInterval(interval);
    }
  }, [busy, state?.plan?.explanation, displayedThinking]);

  // Clear thinking when starting new analysis
  useEffect(() => {
    if (busy === "analyze") {
      setDisplayedThinking("");
    }
  }, [busy]);

  const run = async (label, fn) => {
    setError(null);
    setBusy(label);
    try {
      await fn();
      await loadAll();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err));
    } finally {
      setBusy(null);
    }
  };

  if (!detail || !state) {
    return (
      <div className="page">
        <ErrorBanner message={error} onDismiss={() => setError(null)} />
        {!error && <div className="empty">Loading…</div>}
      </div>
    );
  }

  const issue = detail.issue;
  const rule = detail.rule || {};
  const context = detail.code_context;
  const plan = state.plan;
  const hasPlan = Boolean(plan);

  return (
    <div className="page">
      <div className="page-head">
        <button className="link-back" onClick={() => navigate(-1)}>
          ← Issues
        </button>
      </div>

      <ErrorBanner message={error} onDismiss={() => setError(null)} />

      <section className="card">
        <h1 className="issue-title">{issue.message}</h1>
        <div className="issue-meta">
          <SeverityBadge severity={issue.severity} />
          <span>{issue.type}</span>
          <code className="rule">{issue.rule}</code>
          <span className="loc">
            {issue.file_path}
            {issue.line ? ` line ${issue.line}` : ""}
          </span>
        </div>

        {rule.description && (
          <div className="disclosure">
            <button className="disclosure-toggle" onClick={() => setRuleOpen((v) => !v)}>
              {ruleOpen ? "▾" : "▸"} Why Sonar reported this — {rule.name || rule.key}
            </button>
            {ruleOpen && <pre className="rule-text">{rule.description.slice(0, 6000)}</pre>}
          </div>
        )}

        {detail.repo_error && <div className="banner banner-warn">{detail.repo_error}</div>}

        {context && (
          <>
            <p className="caption">
              {issue.file_path} lines {context.start_line}–{context.end_line} of{" "}
              {context.total_lines}
            </p>
            <pre className="code-block">{context.text}</pre>
          </>
        )}
      </section>

      <div className="button-row">
        <button
          className={hasPlan ? "btn btn-outline" : "btn btn-primary"}
          onClick={() => run("analyze", () => api.analyzeIssue(issueId))}
          disabled={busy === "analyze"}
        >
          {busy === "analyze"
            ? "Analyzing…"
            : hasPlan
            ? "Re-run analysis"
            : "Analyze with AI"}
        </button>
      </div>

      {busy === "analyze" && (
        <section className="card" style={{ borderColor: "#4f46e5", backgroundColor: "#f0f4ff" }}>
          <div style={{ display: "flex", alignItems: "center", gap: "12px", marginBottom: "16px" }}>
            <span style={{ fontSize: "20px", animation: "spin 1s linear infinite" }}>🤖</span>
            <h3 style={{ margin: 0, color: "#4f46e5" }}>Claude is analyzing…</h3>
          </div>
          <p style={{ color: "#666", fontSize: "14px", lineHeight: "1.6", minHeight: "60px", fontFamily: "monospace" }}>
            {displayedThinking || "Starting analysis…"}
            <span style={{ animation: "blink 1s infinite", marginLeft: "4px" }}>▌</span>
          </p>
        </section>
      )}

      {plan && (
        <section className="card">
          <div className="card-head">
            <h2>AI analysis</h2>
            <ConfidenceBar value={plan.confidence} />
          </div>

          <dl className="def-list">
            <dt>What this means</dt>
            <dd>{plan.explanation || "—"}</dd>
            <dt>Root cause</dt>
            <dd>{plan.root_cause || "—"}</dd>
            <dt>Impact</dt>
            <dd>{plan.impact || "—"}</dd>
          </dl>

          {plan.remediation_plan?.length > 0 && (
            <div className="steps">
              <strong>Remediation plan</strong>
              <ol>
                {plan.remediation_plan.map((step, i) => (
                  <li key={i}>{step}</li>
                ))}
              </ol>
            </div>
          )}

          {plan.testing_notes && (
            <dl className="def-list">
              <dt>How to verify</dt>
              <dd>{plan.testing_notes}</dd>
            </dl>
          )}

          {state.awaiting_approval && (
            <div className="approval">
              <p>
                <strong>Approve this plan?</strong> Nothing is written to the
                repository until you do.
              </p>
              <textarea
                className="textarea"
                placeholder="Reviewer notes (optional) — these override the plan where they conflict."
                value={feedback}
                onChange={(e) => setFeedback(e.target.value)}
              />
              <div className="button-row">
                <button
                  className="btn btn-primary"
                  onClick={() => run("approve", () => api.approve(issueId, feedback))}
                  disabled={busy === "approve"}
                >
                  {busy === "approve" ? "Applying fix…" : "Approve and generate fix"}
                </button>
                <button
                  className="btn"
                  onClick={() => run("reject", () => api.reject(issueId, feedback))}
                  disabled={busy === "reject"}
                >
                  {busy === "reject" ? "Rejecting…" : "Reject plan"}
                </button>
              </div>
            </div>
          )}

          {!state.awaiting_approval && plan.status === "rejected" && (
            <p className="caption">You rejected this plan. Re-run the analysis to try again.</p>
          )}
        </section>
      )}

      {["applied", "failed", "fix_generated"].includes(state.status) && (
        <FixSection state={state} />
      )}
    </div>
  );
}

function FixSection({ state }) {
  if (state.status === "failed") {
    return (
      <section className="card">
        <h2>Fix</h2>
        <div className="banner banner-error">{state.error || "The fix could not be generated."}</div>
      </section>
    );
  }

  const fix = state.fix || {};
  const pr = state.pr || {};
  const run = state.run || {};
  const commitMessage = pr.commit_message || run.commit_message;
  let prDescription = run.pr_description;
  if (pr && !prDescription) {
    prDescription = `# ${pr.pr_title || ""}\n\n${pr.pr_description || ""}`;
  }
  const testing = fix.testing_suggestions || run.testing_suggestions;

  return (
    <section className="card">
      <h2>Fix</h2>
      {state.branch && (
        <div className="banner banner-success">
          Changes committed to branch <code>{state.branch}</code>
        </div>
      )}
      {state.commit_sha && <p className="caption">Commit {state.commit_sha}</p>}

      {fix.changes_summary && (
        <dl className="def-list">
          <dt>What changed</dt>
          <dd>{fix.changes_summary}</dd>
        </dl>
      )}

      {state.diff && (
        <>
          <strong>Diff</strong>
          <DiffBlock diff={state.diff} />
        </>
      )}

      {commitMessage && (
        <>
          <strong>Commit message</strong>
          <pre className="code-block">{commitMessage}</pre>
        </>
      )}

      {prDescription && (
        <>
          <strong>PR description</strong>
          <pre className="code-block">{prDescription}</pre>
        </>
      )}

      {testing && (
        <dl className="def-list">
          <dt>Testing suggestions</dt>
          <dd>{testing}</dd>
        </dl>
      )}

      {state.branch && (
        <p className="caption">
          Push it yourself when you are happy:{" "}
          <code>git push -u origin {state.branch}</code>
        </p>
      )}
    </section>
  );
}
