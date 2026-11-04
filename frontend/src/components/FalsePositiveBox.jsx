import { useState } from "react";
import { api, ApiError } from "../api";

// Set an issue aside as a false positive, or move it back. This only changes
// SonarFix: SonarQube is never told, so the issue stays open there. A marked
// issue leaves the Open list, shows under "False positive", and is skipped by
// the fix plan. If the AI suspects a false positive it says so here, but only
// a person can confirm it.
export default function FalsePositiveBox({ issueId, fp, onChanged, onError }) {
  const [reason, setReason] = useState("");
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);

  const act = async (call) => {
    setBusy(true);
    try {
      await call();
      setOpen(false);
      setReason("");
      await onChanged?.();
    } catch (err) {
      onError?.(err instanceof ApiError ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };

  if (fp?.status === "marked") {
    return (
      <section className="fp fp-marked">
        <div className="fp-head">
          <strong>Marked as a false positive</strong>
          <span className="fp-scope">SonarFix only · SonarQube unchanged</span>
        </div>
        {fp.reason && <p className="fp-reason">{fp.reason}</p>}
        <p className="fp-sub">
          {fp.source === "ai" ? "Suggested by the AI and confirmed by you." : "Marked by you."} It is hidden from the
          Open list and will not be planned or fixed.
        </p>
        <div className="button-row">
          <button className="btn btn-sm" disabled={busy} onClick={() => act(() => api.unmarkFalsePositive(issueId))}>
            {busy ? "Moving…" : "Move back to open issues"}
          </button>
        </div>
      </section>
    );
  }

  const suggested = fp?.status === "suggested";
  return (
    <section className={`fp ${suggested ? "fp-suggested" : ""}`}>
      <div className="fp-head">
        <strong>{suggested ? "The AI thinks this is a false positive" : "Not a real problem?"}</strong>
        <span className="fp-scope">SonarFix only · SonarQube unchanged</span>
      </div>
      {suggested && fp.reason && <p className="fp-reason">{fp.reason}</p>}
      {suggested && <p className="fp-sub">Read the reason and the code, then confirm. Nothing moves until you do.</p>}

      {open && (
        <textarea
          className="textarea"
          placeholder="Why is this a false positive? (optional, shown in the False positive tab)"
          value={reason}
          onChange={(e) => setReason(e.target.value)}
        />
      )}
      <div className="button-row">
        {open || suggested ? (
          <button
            className="btn btn-primary btn-sm"
            disabled={busy}
            onClick={() => act(() => api.markFalsePositive(issueId, reason))}
          >
            {busy ? "Saving…" : "Mark as false positive"}
          </button>
        ) : (
          <button className="btn btn-sm" onClick={() => setOpen(true)}>
            Mark as false positive
          </button>
        )}
        {open && !suggested && (
          <button className="btn btn-ghost btn-sm" onClick={() => setOpen(false)}>Cancel</button>
        )}
      </div>
    </section>
  );
}
