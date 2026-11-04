import { useEffect, useRef, useState } from "react";
import { api, ApiError } from "../api";

// Which SonarQube branch SonarFix reads issues from. SonarQube can analyse
// several branches of one project, each with its own issues. The choice is
// remembered per project; changing it re-syncs the issues for that branch.
const ago = (iso) => {
  if (!iso) return "";
  const days = Math.floor((Date.now() - new Date(iso).getTime()) / 86400000);
  if (days <= 0) return "today";
  if (days === 1) return "yesterday";
  if (days < 30) return `${days} days ago`;
  return new Date(iso).toLocaleDateString();
};

function Gate({ status }) {
  if (!status) return null;
  const ok = status === "OK";
  return <span className={`bp-gate ${ok ? "bp-ok" : "bp-fail"}`}>{ok ? "Passed" : "Failed"}</span>;
}

export default function BranchPicker({ projectKey, onChanged, onError }) {
  const [info, setInfo] = useState(null);
  const [failed, setFailed] = useState(null);
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState(null);
  const box = useRef(null);

  useEffect(() => {
    setInfo(null);
    setFailed(null);
    setNote(null);
    api.sonarBranches(projectKey).then(setInfo).catch((err) => setFailed(err instanceof ApiError ? err.message : String(err)));
  }, [projectKey]);

  useEffect(() => {
    if (!open) return undefined;
    const close = (e) => {
      if (box.current && !box.current.contains(e.target)) setOpen(false);
    };
    const esc = (e) => e.key === "Escape" && setOpen(false);
    document.addEventListener("mousedown", close);
    document.addEventListener("keydown", esc);
    return () => {
      document.removeEventListener("mousedown", close);
      document.removeEventListener("keydown", esc);
    };
  }, [open]);

  if (failed) {
    return (
      <div className="bp bp-error">
        <span className="bp-label">Branch</span>
        <span className="bp-hint">Couldn't load SonarQube branches. {failed}</span>
      </div>
    );
  }
  if (!info) return <div className="bp bp-loading"><span className="bp-label">Branch</span><span className="bp-skel" /></div>;

  const current = info.branches.find((b) => b.name === info.selected) || info.branches[0];
  const single = info.branches.length < 2;

  const choose = async (name) => {
    setOpen(false);
    if (name === info.selected) return;
    setBusy(true);
    setNote(null);
    try {
      const next = await api.setSonarBranch(projectKey, name);
      setInfo(next);
      setNote(next.git_note);
      await onChanged?.();
    } catch (err) {
      onError?.(err instanceof ApiError ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="bp" ref={box}>
      <span className="bp-label">Branch</span>
      <div className="bp-wrap">
        <button
          type="button"
          className={`bp-button ${open ? "bp-open" : ""}`}
          disabled={busy || single}
          onClick={() => setOpen(!open)}
          aria-haspopup="listbox"
          aria-expanded={open}
        >
          <svg className="bp-icon" viewBox="0 0 16 16" width="14" height="14" aria-hidden="true">
            <path fill="currentColor" d="M5 3.25a1.75 1.75 0 1 1-2.5 1.58v6.34a1.75 1.75 0 1 1 1.5 0V8.5c0-.97.78-1.75 1.75-1.75h3.5c.14 0 .25-.11.25-.25v-.67A1.75 1.75 0 1 1 11.5 6v.5c0 .97-.78 1.75-1.75 1.75h-3.5a.25.25 0 0 0-.25.25v.2a1.75 1.75 0 0 1-.5-.2V4.83A1.75 1.75 0 0 1 5 3.25Z" />
          </svg>
          <span className="bp-name">{current?.name}</span>
          {current?.is_main && <span className="bp-tag">main</span>}
          <Gate status={current?.status} />
          {!single && <span className="bp-caret" aria-hidden="true">▾</span>}
        </button>

        {open && (
          <ul className="bp-menu" role="listbox">
            {info.branches.map((b) => (
              <li key={b.name} role="option" aria-selected={b.name === info.selected}>
                <button type="button" className={`bp-item ${b.name === info.selected ? "bp-current" : ""}`} onClick={() => choose(b.name)}>
                  <span className="bp-check">{b.name === info.selected ? "✓" : ""}</span>
                  <span className="bp-item-main">
                    <span className="bp-item-name">
                      {b.name}
                      {b.is_main && <span className="bp-tag">main</span>}
                    </span>
                    {b.analysed_at && <span className="bp-item-sub">Analysed {ago(b.analysed_at)}</span>}
                  </span>
                  <Gate status={b.status} />
                </button>
              </li>
            ))}
          </ul>
        )}
      </div>

      {busy && <span className="bp-hint"><span className="bp-spin" /> Loading issues…</span>}
      {!busy && single && <span className="bp-hint">Only branch analysed</span>}
      {!busy && !single && current?.analysed_at && <span className="bp-hint">Analysed {ago(current.analysed_at)}</span>}
      {note && <div className="bp-note">{note}</div>}
    </div>
  );
}
