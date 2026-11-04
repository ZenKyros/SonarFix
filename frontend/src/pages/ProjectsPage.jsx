import { useEffect, useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";
import { api, ApiError } from "../api";
import ErrorBanner from "../components/ErrorBanner";
import { useStatus } from "../status";

// Sonar names are often the raw key ("com.unisys.InfoImage:INF-SRC.IIFBulkDelete"):
// show the readable tail and keep the group as a small tag.
function displayName(project) {
  const name = project.name || project.key;
  const tail = name.includes(":") ? name.split(":").pop() : name;
  const group = name.includes(":") && tail.includes(".") ? tail.split(".")[0] : "";
  return { title: group ? tail.slice(group.length + 1) : tail, group };
}

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
  const [filter, setFilter] = useState("all");
  const [editingRepo, setEditingRepo] = useState(false);

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
    const matching = q
      ? projects.filter(
          (p) => p.name.toLowerCase().includes(q) || p.key.toLowerCase().includes(q)
        )
      : projects;
    const list = filter === "ready" ? matching.filter((p) => p.repo_path) : matching;
    // Projects with a repo already configured are the ones you can act on -
    // surface those first instead of making you scroll a 50-item list.
    return [...list].sort((a, b) => Boolean(b.repo_path) - Boolean(a.repo_path));
  }, [projects, query, filter]);

  const readyCount = projects.filter((p) => p.repo_path).length;

  useEffect(() => {
    setRepoPathDraft(selected?.repo_path || "");
    setEditingRepo(false);
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
    setEditingRepo(false);
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

  const hasRepo = Boolean(selected?.repo_path);
  const hasIssues = Boolean(selected?.last_synced_at);
  // done / current / locked, so the stepper shows where the user is
  const stepClass = (n) => {
    const doneUpTo = !hasRepo ? 0 : !hasIssues ? 1 : 2;
    if (n <= doneUpTo) return "done";
    if (n === doneUpTo + 1) return "current";
    return n === 4 && hasRepo ? "" : "locked";
  };

  const openPlan = () => navigate(`/projects/${selected.key}/plan`);

  return (
    <div className="page">
      <div className="page-head">
        <h1>Projects</h1>
        <p className="subtitle">
          Pick a project, connect its repository, fetch the SonarQube issues, then
          review the plan and let AI fix them.
        </p>
      </div>

      <ErrorBanner message={error} onDismiss={() => setError(null)} />

      <div className="projects-bar">
        <div className="bar-stat"><strong>{projects.length}</strong><span>Projects</span></div>
        <div className="bar-sep" />
        <div className="bar-stat"><strong>{readyCount}</strong><span>Ready to fix</span></div>
        <button className="btn btn-primary" onClick={handleSync} disabled={busy === "sync"}>
          {busy === "sync" ? "Syncing…" : "Sync from SonarQube"}
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
            <div className="filter-tabs">
              <button className={filter === "all" ? "active" : ""} onClick={() => setFilter("all")}>
                All ({projects.length})
              </button>
              <button className={filter === "ready" ? "active" : ""} onClick={() => setFilter("ready")}>
                Ready ({readyCount})
              </button>
            </div>
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
                  <span className="project-name">{displayName(p).title}</span>
                  <span className="project-key">
                    {displayName(p).group && <span className="project-group">{displayName(p).group}</span>}
                    {p.repo_path ? "Repo connected" : "No repo yet"}
                  </span>
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
              <div className="panel-head">
                <div>
                  <h2>{displayName(selected).title}</h2>
                  <p className="mono-key">{selected.key}</p>
                </div>
                <span className={`pill ${hasRepo && hasIssues ? "pill-ready" : "pill-todo"}`}>
                  {hasRepo && hasIssues ? "Ready to fix" : "Setup needed"}
                </span>
              </div>

              <div className={`setup-step ${stepClass(1)}`}>
                <span className="setup-num">{hasRepo ? "✓" : 1}</span>
                <div className="setup-body">
                  <h3>Repository</h3>
                  <p className="caption">A local git clone on the branch SonarQube analysed.</p>
                  {hasRepo && !editingRepo ? (
                    <div className="repo-card">
                      <code>{selected.repo_path}</code>
                      <button className="btn" onClick={() => setEditingRepo(true)}>Change</button>
                    </div>
                  ) : (
                    <div className="input-row">
                      <input
                        className="text-input"
                        placeholder={String.raw`C:\work\my-service`}
                        value={repoPathDraft}
                        onChange={(e) => setRepoPathDraft(e.target.value)}
                      />
                      <button className="btn btn-primary" onClick={handleSaveRepo} disabled={!repoPathDraft.trim() || busy === "save"}>
                        {busy === "save" ? "Saving…" : "Use path"}
                      </button>
                    </div>
                  )}
                  {!hasRepo && (
                    <>
                      <p className="caption or">or clone it (uses the Bitbucket / Azure DevOps token when set)</p>
                      <div className="input-row">
                        <input
                          className="text-input"
                          placeholder="https://bitbucket.example.com/scm/PROJ/repo.git or https://dev.azure.com/org/project/_git/repo"
                          value={cloneUrl}
                          onChange={(e) => setCloneUrl(e.target.value)}
                        />
                        <input
                          className="text-input narrow"
                          placeholder="branch"
                          value={cloneBranch}
                          onChange={(e) => setCloneBranch(e.target.value)}
                        />
                        <button className="btn btn-primary" onClick={handleClone} disabled={!cloneUrl.trim() || busy === "clone-url"}>
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

              <div className={`setup-step ${stepClass(2)}`}>
                <span className="setup-num">{hasIssues ? "✓" : 2}</span>
                <div className="setup-body">
                  <h3>Issues</h3>
                  <p className="caption">
                    {hasIssues ? `Last fetched ${selected.last_synced_at}` : "Not fetched yet. Connect the repository first, then fetch."}
                  </p>
                  <button
                    className={`btn ${hasRepo && !hasIssues ? "btn-primary" : ""}`}
                    onClick={handleFetchIssues}
                    disabled={busy === "fetch"}
                  >
                    {busy === "fetch" ? "Fetching…" : hasIssues ? "Fetch again" : "Fetch issues from SonarQube"}
                  </button>
                </div>
              </div>

              <div className={`setup-step ${stepClass(3)}`}>
                <span className="setup-num">3</span>
                <div className="setup-body">
                  <h3>Fix with AI</h3>
                  <p className="caption">Groups similar issues so the AI can fix each group together. Nothing runs until you choose.</p>
                  <div className="button-row">
                    <button
                      className="btn btn-primary"
                      onClick={openPlan}
                      disabled={!selected.repo_path || !selected.last_synced_at}
                    >
                      Open fix plan
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

              <div className={`setup-step ${stepClass(4)}`}>
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
