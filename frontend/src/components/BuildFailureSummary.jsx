import { useState } from "react";

// Why a build failed, laid out like Visual Studio's Error List plus the part
// VS never gives you: a plain verdict (was it the code change or the machine?),
// why each kind of error happens, how to prevent it, and recommended actions.
// An "ai" recommendation has a button that starts a fresh AI attempt with that
// recommendation (and any extra instructions) as its brief.
const fileName = (path) => (path ? path.replace(/\\/g, "/").split("/").pop() : "");

function Recommendation({ rec, onApply, busy }) {
  const [extra, setExtra] = useState("");
  const [open, setOpen] = useState(false);
  const isAi = rec.kind === "ai";
  return (
    <div className={`rec ${rec.recommended ? "rec-top" : ""}`}>
      <div className="rec-head">
        <strong>{rec.title}</strong>
        {rec.recommended && <span className="rec-badge">Recommended</span>}
        <span className="rec-kind">{isAi ? "AI will do this" : "You do this"}</span>
      </div>
      <p className="rec-why">{rec.why}</p>
      {!isAi && rec.steps && (
        <ol className="fail-steps">
          {rec.steps.map((step) => <li key={step}>{step}</li>)}
        </ol>
      )}
      {isAi && (
        <>
          <button type="button" className="rec-toggle" onClick={() => setOpen(!open)}>
            {open ? "Hide" : "Show"} what the AI will be told, and add your own instructions
          </button>
          {open && (
            <div className="rec-prompt">
              <pre>{rec.prompt}</pre>
              <textarea
                className="textarea"
                placeholder="Extra instructions for the AI (optional) - e.g. “use the existing helper in Common.dll instead of adding a new one”"
                value={extra}
                onChange={(e) => setExtra(e.target.value)}
              />
            </div>
          )}
          <div className="button-row">
            <button
              className="btn btn-primary btn-sm"
              disabled={busy}
              onClick={() => onApply(extra.trim() ? `${rec.prompt}\n\n## Reviewer instructions\n${extra.trim()}` : rec.prompt)}
            >
              {busy ? "Starting…" : "Apply with AI"}
            </button>
          </div>
        </>
      )}
    </div>
  );
}

function ErrorList({ errors }) {
  const [mineOnly, setMineOnly] = useState(false);
  const mine = errors.filter((e) => e.in_changed).length;
  const shown = mineOnly ? errors.filter((e) => e.in_changed) : errors;
  return (
    <div className="errlist">
      <div className="errlist-bar">
        <strong>Error List</strong>
        <span className="errlist-count">{errors.length} error{errors.length === 1 ? "" : "s"}</span>
        <label className="errlist-filter">
          <input type="checkbox" checked={mineOnly} onChange={(e) => setMineOnly(e.target.checked)} />
          Only files the fix changed ({mine})
        </label>
      </div>
      <div className="errlist-scroll">
        <table>
          <thead>
            <tr><th /><th>Code</th><th>Description</th><th>File</th><th>Line</th></tr>
          </thead>
          <tbody>
            {shown.length === 0 && (
              <tr><td colSpan={5} className="errlist-empty">No errors in the files the fix changed.</td></tr>
            )}
            {shown.map((e, i) => (
              <tr key={i} className={e.in_changed ? "errlist-mine" : ""}>
                <td><span className="errlist-x" title="Error">✕</span></td>
                <td className="mono">{e.code}</td>
                <td>{e.message}</td>
                <td className="mono" title={e.file || ""}>
                  {fileName(e.file)}
                  {e.in_changed && <span className="errlist-tag">changed by fix</span>}
                </td>
                <td className="mono">{e.line ?? ""}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

export default function BuildFailureSummary({ summary, onApply, busy }) {
  if (!summary) return null;
  const recs = summary.recommendations || [];
  return (
    <section className={`fail-box fail-${summary.cause}`}>
      <span className="fail-cause">{summary.cause_label}</span>
      <p className="fail-headline">{summary.headline}</p>

      {summary.errors?.length > 0 && <ErrorList errors={summary.errors} />}

      <span className="fail-label">Why it happened and how to prevent it</span>
      <div className="fail-reasons">
        {summary.reasons.map((reason) => (
          <div key={reason.title} className="fail-reason">
            <div className="fail-reason-head">
              <strong>{reason.title}</strong>
              <span className="fail-count">
                {reason.count} error{reason.count === 1 ? "" : "s"}
              </span>
            </div>
            <p className="fail-plain">{reason.plain}</p>
            {reason.names.length > 0 && (
              <p className="fail-names">
                Missing: {reason.names.map((n) => <code key={n}>{n}</code>)}
              </p>
            )}
            {reason.prevent && (
              <p className="fail-prevent"><strong>Prevent it:</strong> {reason.prevent}</p>
            )}
          </div>
        ))}
      </div>

      {recs.length > 0 && (
        <>
          <span className="fail-label">Recommended actions</span>
          <div className="recs">
            {recs.map((rec) => (
              <Recommendation key={rec.id} rec={rec} busy={busy} onApply={(text) => onApply?.(text)} />
            ))}
          </div>
        </>
      )}
    </section>
  );
}
