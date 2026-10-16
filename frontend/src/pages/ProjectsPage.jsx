import { useEffect, useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";
import { api, ApiError } from "../api";
import ErrorBanner from "../components/ErrorBanner";
import { useStatus } from "../status";

export default function ProjectsPage() {
  const navigate = useNavigate();
  const { track } = useStatus();
  const [projects, setProjects] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [selectedKey, setSelectedKey] = useState(null);
  const [repoPathDraft, setRepoPathDraft] = useState("");
  const [cloneUrl, setCloneUrl] = useState("");
  const [cloneBranch, setCloneBranch] = useState("");
  const [busy, setBusy] = useState(null); // which action is in flight
  const [query, setQuery] = useState("");

  const load = async () => {
    try {
      setLoading(true);
      const data = await api.listProjects();
      setProjects(data);
      if (!selectedKey && data.length) setSelectedKey(data[0].key);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err));
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const selected = projects.find((p) => p.key === selectedKey) || null;

  const filteredProjects = useMemo(() => {
    const q = query.trim().toLowerCase();
    const list = q
      ? projects.filter(
          (p) => p.name.toLowerCase().includes(q) || p.key.toLowerCase().includes(q)
        )
      : projects;
    // Projects with a repo already configured are the ones you can act on -
    // surface those first instead of making you scroll a 50-item list.
    return [...list].sort((a, b) => Boolean(b.repo_path) - Boolean(a.repo_path));
  }, [projects, query]);

  useEffect(() => {
    setRepoPathDraft(selected?.repo_path || "");
  }, [selectedKey]); // eslint-disable-line react-hooks/exhaustive-deps

  const run = async (busyKey, statusLabel, fn) => {
    setError(null);
    setBusy(busyKey);
    try {
      await track(statusLabel, fn);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err));
    } finally {
      setBusy(null);
    }
  };

  const handleSync = () => run("sync", "Syncing projects from SonarQube…", async () => {
    const data = await api.syncProjects();
    setProjects(data);
  });

  const handleAutoClone = () => run("clone", `Discovering repo for ${selected.name}…`, async () => {
    await api.autoClone(selected.key);
    await load();
  });

  const handleSaveRepo = () => run("save", `Saving repo path for ${selected.name}…`, async () => {
    await api.setRepoPath(selected.key, repoPathDraft.trim());
    await load();
  });

  const handleClone = () => run("clone-url", `Cloning ${cloneUrl.trim()}…`, async () => {
    await api.cloneRepo(selected.key, cloneUrl.trim(), cloneBranch.trim());
    setCloneUrl("");
    await load();
  });

  const handleFetchIssues = () => run("fetch", `Fetching issues for ${selected.name}…`, async () => {
    await api.syncIssues(selected.key);
    await load();
  });

  const openPlan = () => navigate(`/projects/${selected.key}/plan`);

  return (
    <div className="page">
      <div className="page-head">
        <h1>Projects</h1>
        <p className="subtitle">
          Sync your SonarQube projects, point one at its repository, fetch the
          issues, then review a plan that fixes what it can without AI.
        </p>
      </div>

      <ErrorBanner message={error} onDismiss={() => setError(null)} />

      <div className="toolbar">
        <p className="caption" style={{ margin: 0 }}>
          Projects already seen on this SonarQube server:
        </p>
        <button className="btn btn-primary" onClick={handleSync} disabled={busy === "sync"}>
          {busy === "sync" ? "Syncing…" : "Sync projects from SonarQube"}
        </button>
      </div>

      {loading ? (
        <div className="empty">Loading projects…</div>
      ) : projects.length === 0 ? (
        <div className="empty">No projects yet. Sync from SonarQube to get started.</div>
      ) : (
        <div className="split">
          <div className="project-list-col">
            <input
              className="text-input"
              placeholder={`Search ${projects.length} projects…`}
              value={query}
              onChange={(e) => setQuery(e.target.value)}
            />
            <p className="caption project-list-hint">
              <span className="dot dot-ok" /> repo configured — ready to fetch issues
            </p>
            {filteredProjects.length === 0 ? (
              <div className="empty">No project matches "{query}".</div>
            ) : (
          <ul className="project-list">
            {filteredProjects.map((p) => (
              <li key={p.key}>
                <button
                  className={`project-row ${p.key === selectedKey ? "active" : ""}`}
                  onClick={() => setSelectedKey(p.key)}
                >
                  <span className="project-name">{p.name}</span>
                  <span className="project-key">{p.key}</span>
                  {p.repo_path ? (
                    <span className="dot dot-ok" title="Repo configured" />
                  ) : (
                    <span className="dot dot-off" title="No repo configured" />
                  )}
                </button>
              </li>
            ))}
          </ul>
            )}
          </div>

          {selected && (
            <div className="panel">
              <h2>{selected.name}</h2>
              <p className="mono-key">{selected.key}</p>

              <div className="setup-step">
                <span className="setup-num">1</span>
                <div className="setup-body">
                  <h3>Repository</h3>
                  <p className="caption">A local git clone on the branch SonarQube analysed.</p>
                  <div className="input-row">
                    <input
                      className="text-input"
                      placeholder={String.raw`C:\work\my-service`}
                      value={repoPathDraft}
                      onChange={(e) => setRepoPathDraft(e.target.value)}
                    />
                    <button className="btn" onClick={handleSaveRepo} disabled={!repoPathDraft.trim() || busy === "save"}>
                      {busy === "save" ? "Saving…" : "Use path"}
                    </button>
                  </div>
                  {!selected.repo_path && (
                    <>
                      <p className="caption or">or clone it (uses BITBUCKET_TOKEN when set)</p>
                      <div className="input-row">
                        <input
                          className="text-input"
                          placeholder="https://bitbucket.example.com/scm/PROJ/repo.git"
                          value={cloneUrl}
                          onChange={(e) => setCloneUrl(e.target.value)}
                        />
                        <input
                          className="text-input narrow"
                          placeholder="branch"
                          value={cloneBranch}
                          onChange={(e) => setCloneBranch(e.target.value)}
                        />
                        <button className="btn" onClick={handleClone} disabled={!cloneUrl.trim() || busy === "clone-url"}>
                          {busy === "clone-url" ? "Cloning…" : "Clone"}
                        </button>
                      </div>
                      <button className="link-btn" onClick={handleAutoClone} disabled={busy === "clone"}>
                        {busy === "clone" ? "Discovering…" : "Or discover the repo from SonarQube's binding"}
                      </button>
                    </>
                  )}
                </div>
              </div>

              <div className="setup-step">
                <span className="setup-num">2</span>
                <div className="setup-body">
                  <h3>Issues</h3>
                  <p className="caption">
                    {selected.last_synced_at ? `Last fetched ${selected.last_synced_at}` : "Not fetched yet."}
                  </p>
                  <button className="btn" onClick={handleFetchIssues} disabled={busy === "fetch"}>
                    {busy === "fetch" ? "Fetching…" : "Fetch issues from SonarQube"}
                  </button>
                </div>
              </div>

              <div className="setup-step">
                <span className="setup-num">3</span>
                <div className="setup-body">
                  <h3>Remediation plan</h3>
                  <p className="caption">Groups issues and routes each to a mechanical recipe, AI, or skip. No AI runs yet.</p>
                  <div className="button-row">
                    <button
                      className="btn btn-primary"
                      onClick={openPlan}
                      disabled={!selected.repo_path || !selected.last_synced_at}
                    >
                      Open remediation plan
                    </button>
                    <button
                      className="btn btn-ghost"
                      disabled={!selected.last_synced_at}
                      onClick={() => navigate(`/projects/${selected.key}/issues`, { state: { projectName: selected.name } })}
                    >
                      All issues
                    </button>
                    <a className="btn btn-ghost" href={api.reportUrl(selected.key)} download>
                      HTML report
                    </a>
                  </div>
                </div>
              </div>

              <div className="setup-step">
                <span className="setup-num">4</span>
                <div className="setup-body">
                  <h3>Dependencies</h3>
                  <p className="caption">
                    Checks every package this project depends on against known CVEs, and
                    whether the fix is a safe (patch/minor) or risky (major) version bump.
                  </p>
                  <button
                    className="btn btn-ghost"
                    disabled={!selected.repo_path}
                    onClick={() =>
                      navigate(`/projects/${selected.key}/dependencies`, {
                        state: { projectName: selected.name },
                      })
                    }
                  >
                    View dependencies
                  </button>
                </div>
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
