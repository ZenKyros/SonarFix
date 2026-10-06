import { Link } from "react-router-dom";
import { useEffect, useState } from "react";
import { api } from "../api";
import ActivityBar from "./ActivityBar";

// "claude-code / claude-haiku-4-5 (acceptEdits) | sonar-mcp: stdio: ..." -> "Claude Code · Haiku"
function shortEngineLabel(wiring) {
  const engine = wiring.split("/")[0]?.trim();
  const model = wiring.match(/\/\s*([\w.-]+)/)?.[1] || "";
  const modelShort = model.replace(/^claude-/, "").replace(/-\d.*$/, "");
  const engineLabel = engine === "claude-code" ? "Claude Code" : engine || "AI engine";
  return modelShort ? `${engineLabel} · ${modelShort}` : engineLabel;
}

export default function Layout({ children }) {
  const [wiring, setWiring] = useState(null);
  const [ok, setOk] = useState(true);

  useEffect(() => {
    api
      .health()
      .then((res) => {
        setWiring(res.engine);
        setOk(!res.engine?.toLowerCase().startsWith("misconfigured"));
      })
      .catch(() => {
        setWiring(null);
        setOk(false);
      });
  }, []);

  return (
    <div className="shell">
      <header className="topbar">
        <Link to="/" className="brand">
          <span className="brand-mark">U</span>
          <span className="brand-text">
            <span className="brand-org">Unisys</span>
            <span className="brand-product">SonarFix</span>
          </span>
        </Link>
        {wiring && (
          <span
            className={`status-pill ${ok ? "status-ok" : "status-bad"}`}
            title={wiring}
          >
            <span className="status-dot" />
            {ok ? shortEngineLabel(wiring) : "AI engine not configured"}
          </span>
        )}
      </header>
      <ActivityBar />
      <main className="content">{children}</main>
      <footer className="app-footer">Unisys &middot; Internal use only &middot; AI-assisted remediation, human-approved</footer>
    </div>
  );
}
