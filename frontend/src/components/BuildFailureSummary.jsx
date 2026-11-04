// Why a build failed, in plain words: the verdict (was it the code change or
// the machine?), each kind of problem with a couple of real examples, and
// what to do next. The raw log stays available below it for anyone who wants it.
const fileName = (path) => (path ? path.replace(/\\/g, "/").split("/").pop() : "");

export default function BuildFailureSummary({ summary }) {
  if (!summary) return null;
  return (
    <section className={`fail-box fail-${summary.cause}`}>
      <span className="fail-cause">{summary.cause_label}</span>
      <p className="fail-headline">{summary.headline}</p>

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
            <ul className="fail-examples">
              {reason.examples.map((e, i) => (
                <li key={i}>
                  {e.file && (
                    <code>
                      {fileName(e.file)}
                      {e.line ? `:${e.line}` : ""}
                    </code>
                  )}
                  <span className="fail-code">{e.code}</span>
                  <span>{e.message}</span>
                </li>
              ))}
            </ul>
          </div>
        ))}
      </div>

      {summary.next_steps.length > 0 && (
        <>
          <span className="fail-label">What to do</span>
          <ol className="fail-steps">
            {summary.next_steps.map((step) => <li key={step}>{step}</li>)}
          </ol>
        </>
      )}
    </section>
  );
}
