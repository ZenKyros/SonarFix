import { useEffect, useState } from "react";
import { api, ApiError } from "../api";

// Where the fix lives in the user's own repo, and a one-click way to put it
// on disk so it can be opened and built in Visual Studio. The fix is always
// committed on its own branch; it only shows up in the working folder while
// that branch is checked out.
export default function LocalReview({ batchId }) {
  const [info, setInfo] = useState(null);
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);
  const [copied, setCopied] = useState(false);

  useEffect(() => {
    api.batchLocal(batchId).then(setInfo).catch((err) => setError(err instanceof ApiError ? err.message : String(err)));
  }, [batchId]);

  const act = async (call) => {
    setBusy(true);
    setError(null);
    try {
      setInfo(await call(batchId));
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };

  const copy = async () => {
    try {
      await navigator.clipboard.writeText(info.commands.join("\n"));
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch {
      // clipboard blocked - the commands are still on screen
    }
  };

  if (!info && !error) return null;
  return (
    <section className="card">
      <div className="card-head">
        <h2>Review and build locally</h2>
        {info && <span className="chip">{info.on_branch ? "Your repo is on the fix branch" : `Your repo is on ${info.current_branch}`}</span>}
      </div>
      {error && <div className="banner banner-error">{error}</div>}
      {info && (
        <>
          <p className="caption">
            The fix is committed on branch <code>{info.branch}</code> in <code>{info.repo_path}</code>.
            It only appears in your folder (and in Visual Studio) while that branch is checked out.
            The <em>Git Changes</em> panel in Visual Studio lists uncommitted edits only, so use{" "}
            <strong>Git &gt; Manage Branches &gt; Compare with Current Branch</strong> to see the diff.
          </p>
          <div className="button-row">
            {info.on_branch ? (
              <button className="btn" disabled={busy} onClick={() => act(api.batchReturn)}>
                {busy ? "Switching…" : `Switch my repo back to ${info.base_branch}`}
              </button>
            ) : (
              <button className="btn btn-primary" disabled={busy} onClick={() => act(api.batchCheckout)}>
                {busy ? "Switching…" : "Switch my repo to the fix branch"}
              </button>
            )}
            <button className="btn btn-ghost" onClick={copy}>{copied ? "Copied" : "Copy git commands"}</button>
          </div>
          {info.on_branch && (
            <p className="caption">
              Done. In Visual Studio, reload if asked, open the solution, and Build. Changed files:
            </p>
          )}
          {info.files.length > 0 && (
            <ul className="fail-examples">
              {info.files.map((f) => <li key={f}><code>{f}</code></li>)}
            </ul>
          )}
        </>
      )}
    </section>
  );
}
