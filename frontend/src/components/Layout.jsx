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
  const [modelInfo, setModelInfo] = useState(null);
  const [switching, setSwitching] = useState(false);

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
    api.getModelSettings().then(setModelInfo).catch(() => {});
  }, []);

  const handleModelChange = async (e) => {
    const model = e.target.value;
    setSwitching(true);
    try {
      const res = await api.setModel(model);
      setModelInfo((prev) => ({ ...prev, model: res.model }));
    } catch {
      // Leave the dropdown as-is; the next health/model check will resync it.
    } finally {
      setSwitching(false);
    }
  };

  return (
    <div className="shell">
      <header className="topbar">
        <Link to="/" className="brand">
          <img src="/Logo.webp" alt="Unisys" className="brand-logo" />
          <span className="brand-text">
            <span className="brand-product">SonarFix</span>
          </span>
        </Link>
        {modelInfo?.choices?.length > 0 && (
          <select
            className="model-picker"
            value={modelInfo.model}
            onChange={handleModelChange}
            disabled={switching}
            title="Claude model used for new analysis and fix sessions"
          >
            {modelInfo.choices.map((c) => (
              <option key={c.id} value={c.id}>
                {c.label}
              </option>
            ))}
          </select>
        )}
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
