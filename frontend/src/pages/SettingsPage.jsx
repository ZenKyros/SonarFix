import { useCallback, useEffect, useState } from "react";
import { api } from "../api";
import ErrorBanner from "../components/ErrorBanner";

const EMPTY = {
  sonar_url: "",
  sonar_token: "",
  sonar_org: "",
  bitbucket_username: "",
  bitbucket_token: "",
};

function Step({ n, title, done, children }) {
  return (
    <section className="card" style={{ marginBottom: 16 }}>
      <div className="card-head">
        <h2 style={{ margin: 0 }}>
          {done ? "✓" : n}. {title}
        </h2>
      </div>
      {children}
    </section>
  );
}

export default function SettingsPage() {
  const [status, setStatus] = useState(null);
  const [form, setForm] = useState(EMPTY);
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");

  const refresh = useCallback(async () => {
    try {
      const next = await api.setupStatus();
      setStatus(next);
      setForm((f) => ({
        ...f,
        sonar_url: f.sonar_url || next.credentials.sonar_url,
        sonar_org: f.sonar_org || next.credentials.sonar_org,
        bitbucket_username: f.bitbucket_username || next.credentials.bitbucket_username,
      }));
    } catch (err) {
      setError(err.message);
    }
  }, []);

  useEffect(() => {
    refresh();
  }, [refresh]);

  // After "Sign in", the browser flow finishes outside this page - keep checking.
  useEffect(() => {
    if (!status || !status.claude.installed || status.claude.logged_in) return undefined;
    const timer = setInterval(refresh, 4000);
    return () => clearInterval(timer);
  }, [status, refresh]);

  const run = async (name, fn, message) => {
    setBusy(name);
    setError("");
    setNotice("");
    try {
      await fn();
      if (message) setNotice(message);
      await refresh();
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy("");
    }
  };

  const set = (key) => (e) => setForm({ ...form, [key]: e.target.value });
  const save = () =>
    run(
      "save",
      async () => {
        await api.saveCredentials(form);
        setForm((f) => ({ ...f, sonar_token: "", bitbucket_token: "" }));
      },
      "Saved."
    );
  const testSonar = () =>
    run("test", async () => {
      await api.saveCredentials(form);
      const result = await api.testSonar();
      setNotice(`SonarQube connected - ${result.projects} projects visible.`);
    });

  if (!status) return <div className="panel">Loading… <ErrorBanner message={error} /></div>;
  const { claude, credentials } = status;

  return (
    <div style={{ maxWidth: 720 }}>
      <h1>Settings</h1>
      <ErrorBanner message={error} onDismiss={() => setError("")} />
      {notice && <div className="banner">{notice}</div>}

      <Step n={1} title="Claude Code (AI engine)" done={claude.installed && claude.logged_in}>
        <p>
          SonarFix uses your own Claude sign-in (SSO) - no API key needed.
        </p>
        {!claude.installed && (
          <button
            className="btn btn-primary"
            disabled={!!busy}
            onClick={() => run("install", api.installClaude, "Claude Code installed.")}
          >
            {busy === "install" ? "Installing… (can take a minute)" : "Install Claude Code"}
          </button>
        )}
        {claude.installed && !claude.logged_in && (
          <>
            <p>Installed ({claude.version}). Sign in with your company account:</p>
            <div className="button-row">
              <button
                className="btn btn-primary"
                disabled={!!busy}
                onClick={() =>
                  run("login", api.loginClaude, "A sign-in window opened - finish it in your browser.")
                }
              >
                Sign in with Claude
              </button>
              <button className="btn btn-outline" onClick={refresh}>Refresh</button>
            </div>
          </>
        )}
        {claude.installed && claude.logged_in && (
          <>
          <p>
            Signed in{claude.email ? ` as ${claude.email}` : ""} ({claude.version}).{" "}
            <button className="link-btn" onClick={() => run("login", api.loginClaude)}>
              Switch account
            </button>
          </p>
          <p>
            <button
              className="btn btn-outline"
              disabled={!!busy}
              onClick={() =>
                run("tclaude", async () => {
                  const r = await api.testClaude();
                  setNotice(`Claude replied: ${r.reply}`);
                })
              }
            >
              {busy === "tclaude" ? "Testing…" : "Test Claude"}
            </button>
          </p>
          </>
        )}
      </Step>

      <Step n={2} title="SonarQube" done={!!credentials.sonar_url && credentials.sonar_token_set}>
        <label className="field-label">Server URL</label>
        <input
          className="text-input"
          placeholder="https://sonar.yourcompany.com"
          value={form.sonar_url}
          onChange={set("sonar_url")}
        />
        <label className="field-label">
          Token {credentials.sonar_token_set && "(saved - leave blank to keep)"}
        </label>
        <input
          className="text-input"
          type="password"
          autoComplete="off"
          placeholder={credentials.sonar_token_set ? "••••••••" : "My Account > Security > Generate Token"}
          value={form.sonar_token}
          onChange={set("sonar_token")}
        />
        <label className="field-label">Organization (SonarCloud only)</label>
        <input className="text-input" value={form.sonar_org} onChange={set("sonar_org")} />
        <div className="button-row" style={{ marginTop: 12 }}>
          <button className="btn btn-outline" disabled={!!busy} onClick={testSonar}>
            {busy === "test" ? "Testing…" : "Save & test connection"}
          </button>
        </div>
      </Step>

      <Step n={3} title="Bitbucket (for pull requests)" done={credentials.bitbucket_token_set}>
        <label className="field-label">Username (only for app passwords / basic auth)</label>
        <input
          className="text-input"
          value={form.bitbucket_username}
          onChange={set("bitbucket_username")}
        />
        <label className="field-label">
          Access token {credentials.bitbucket_token_set && "(saved - paste a new one to update)"}
        </label>
        <input
          className="text-input"
          type="password"
          autoComplete="off"
          placeholder={credentials.bitbucket_token_set ? "••••••••" : "Repository or project access token"}
          value={form.bitbucket_token}
          onChange={set("bitbucket_token")}
        />
      </Step>

      <button className="btn btn-primary" disabled={!!busy} onClick={save}>
        {busy === "save" ? "Saving…" : "Save settings"}
      </button>
      {status.ready && (
        <p style={{ marginTop: 16 }}>
          All set - go to <a href="/">Projects</a> and sync from SonarQube.
        </p>
      )}
    </div>
  );
}
