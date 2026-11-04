import { useEffect, useRef, useState } from "react";
import { api } from "../api";

// Timeline of everything SonarFix and the AI did for a batch: tasks started
// and finished, AI sessions (with what they were asked and how long they
// took), builds and reviewer decisions. Polls while `live` is true.
const KINDS = { all: "Everything", task: "Tasks", ai: "AI", build: "Build", batch: "Batch", review: "Decisions" };
const time = (iso) => new Date(iso).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
const took = (ms) => (ms == null ? "" : ms < 1000 ? `${ms} ms` : `${(ms / 1000).toFixed(1)} s`);

export default function ActivityLog({ batchId, live }) {
  const [events, setEvents] = useState([]);
  const [kind, setKind] = useState("all");
  const [open, setOpen] = useState({});
  const lastId = useRef(0);

  useEffect(() => {
    let timer;
    let cancelled = false;
    lastId.current = 0;
    setEvents([]);
    const load = async () => {
      try {
        const fresh = await api.getActivity({ batch_id: batchId, after_id: lastId.current });
        if (cancelled) return;
        if (fresh.length) {
          lastId.current = fresh[fresh.length - 1].id;
          setEvents((old) => [...old, ...fresh]);
        }
      } catch {
        // the log is a convenience; never block the page on it
      }
      if (!cancelled && live) timer = setTimeout(load, 2000);
    };
    load();
    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
  }, [batchId, live]);

  const shown = kind === "all" ? events : events.filter((e) => e.kind === kind);
  if (events.length === 0 && !live) return null;

  return (
    <section className="card">
      <div className="card-head">
        <h2>Activity log</h2>
        <span className="chip">{events.length} event{events.length === 1 ? "" : "s"}{live ? " · live" : ""}</span>
      </div>
      <div className="act-filter">
        {Object.entries(KINDS).map(([key, label]) => (
          <button key={key} className={`chip chip-btn ${kind === key ? "chip-on" : ""}`} onClick={() => setKind(key)}>
            {label}
          </button>
        ))}
      </div>
      <ol className="act">
        {shown.length === 0 && <li className="act-empty">Waiting for activity…</li>}
        {shown.map((e) => (
          <li key={e.id} className={`act-item act-${e.level}`}>
            <span className="act-dot" />
            <div className="act-main">
              <div className="act-line">
                <span className="act-time">{time(e.at)}</span>
                <span className={`act-kind act-kind-${e.kind}`}>{e.kind}</span>
                <span className="act-title">{e.title}</span>
                {e.duration_ms != null && <span className="act-took">{took(e.duration_ms)}</span>}
                {e.detail && (
                  <button className="rec-toggle" onClick={() => setOpen({ ...open, [e.id]: !open[e.id] })}>
                    {open[e.id] ? "hide" : "details"}
                  </button>
                )}
              </div>
              {open[e.id] && <pre className="act-detail">{e.detail}</pre>}
            </div>
          </li>
        ))}
      </ol>
    </section>
  );
}
