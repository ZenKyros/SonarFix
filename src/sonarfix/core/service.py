"""Use cases. The API and the UI both go through this module, never deeper."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from langgraph.types import Command

from . import graph as workflow
from . import issues as issues_
from . import sonar, store
from .config import ConfigError, get_settings
from .repo import RepoError, RepoWorkspace


class ServiceError(RuntimeError):
    """Something the user can fix: bad config, missing repo, unknown issue."""


def _git(*args: str, cwd: str | Path | None = None) -> str:
    """Run a git command and return stdout, raise on non-zero."""
    result = subprocess.run(
        ["git", *args], capture_output=True, text=True, cwd=cwd, timeout=300
    )
    if result.returncode != 0:
        raise ServiceError(f"git {' '.join(args)} failed: {result.stderr}")
    return result.stdout


# --- projects and issues -----------------------------------------------------


def sync_projects() -> list[dict[str, Any]]:
    """Pull the project list from Sonar into SQLite and return what we now hold."""
    store.upsert_projects(sonar.list_projects())
    store.upsert_projects(
        [{"key": key, "name": cfg["name"]} for key, cfg in issues_.COMPOSITE_PROJECTS.items()]
    )
    return store.list_projects()


def list_projects() -> list[dict[str, Any]]:
    return store.list_projects()


def set_repo_path(project_key: str, repo_path: str) -> dict[str, Any]:
    if not store.get_project(project_key):
        raise ServiceError(f"Unknown project: {project_key}")
    try:
        RepoWorkspace(repo_path)  # validated eagerly so the UI can report it
    except RepoError as exc:
        raise ServiceError(str(exc)) from exc
    store.set_repo_path(project_key, repo_path)
    project = store.get_project(project_key)
    assert project is not None
    return project


def sync_issues(project_key: str) -> dict[str, Any]:
    """Refresh the local snapshot the Batch/Plan screen plans against.

    The single-issue screens (list/detail/analyze/fix/PR) never read this -
    they always fetch live via core/issues.py, so they can't go stale. This
    snapshot exists only because the Batch/Plan screen (and the PowerShell
    agentcli bridge) cluster issues against actual file content and need a
    stable set to plan against; it's a deliberate, user-triggered refresh,
    not an implicit cache used for display.
    """
    if not store.get_project(project_key):
        raise ServiceError(f"Unknown project: {project_key}. Sync projects first.")
    composite = issues_.COMPOSITE_PROJECTS.get(project_key)
    if composite:
        raws = sonar.list_issues(
            ",".join(composite["component_keys"]), branch=composite["branch"]
        )
        prefix = composite.get("subfolder_prefix")
        if prefix:
            for issue in raws:
                sub_project = issue.get("project") or ""
                if sub_project.startswith(prefix):
                    subfolder = sub_project[len(prefix) :]
                    rest = store.strip_component_prefix(
                        issue.get("component") or "", sub_project
                    )
                    issue["component"] = f"{sub_project}:{subfolder}/{rest}"
    else:
        raws = sonar.list_issues(project_key)
    count = store.replace_issues(project_key, raws)
    store.mark_synced(project_key)
    return {"project_key": project_key, "issues_synced": count}


def ensure_repo(
    project_key: str, *, workdir: Path | None = None
) -> str:
    """Return a local clone of the repo bound to this Sonar project, cloning
    or updating it if needed. Raises ConfigError if no binding is found and
    no repo_path is already registered."""
    project = store.get_project(project_key)
    if project and project.get("repo_path"):
        repo_path = Path(project["repo_path"])
        if repo_path.exists():
            try:
                _git("fetch", "origin", cwd=str(repo_path))
                _git("reset", "--hard", "origin/HEAD", cwd=str(repo_path))
            except ServiceError:
                pass  # Already updated or fetch failed; either way, use it.
            return str(repo_path)

    binding = sonar.get_repo_binding(project_key)
    clone_url = sonar.clone_url_for_binding(binding) if binding else None
    if not clone_url:
        raise ConfigError(
            f"No repository is bound to Sonar project {project_key!r} "
            "(or the token lacks permission to see it). "
            "Pass repo_path explicitly via service.set_repo_path()."
        )

    workdir = workdir or (get_settings().db_path.parent / "repos")
    dest = workdir / project_key
    dest.parent.mkdir(parents=True, exist_ok=True)

    if dest.exists():
        _git("fetch", "origin", cwd=str(dest))
        _git("reset", "--hard", "origin/HEAD", cwd=str(dest))
    else:
        _git("clone", clone_url, str(dest))

    store.set_repo_path(project_key, str(dest))
    return str(dest)


def list_issues(
    project_key: str, severity: str | None = None, issue_type: str | None = None
) -> list[dict[str, Any]]:
    """Live from SonarQube every time - never a local cache that can go stale.

    Each issue is stamped with `pr_url`/`has_pr` from our own local history,
    so the UI can set aside anything that already has an open PR instead of
    re-analysing it.
    """
    issues = issues_.filter_and_sort(issues_.fetch_many(project_key), severity, issue_type)
    pr_status = store.pr_status_by_issue(project_key)
    for issue in issues:
        found = pr_status.get(issue["id"])
        issue["pr_url"] = found["pr_url"] if found else None
        issue["has_pr"] = bool(found)
    return issues


def issue_facets(project_key: str) -> dict[str, list[str]]:
    return issues_.facets(project_key)


def issue_detail(issue_id: str) -> dict[str, Any]:
    """Issue row, rule description and the code around the reported line."""
    issue = issues_.fetch_one(issue_id)
    if not issue:
        raise ServiceError(f"Unknown issue: {issue_id}")

    project = store.get_project(issue["project_key"]) or {}
    detail: dict[str, Any] = {
        "issue": issue,
        "project": project,
        "rule": None,
        "code_context": None,
        "repo_error": None,
    }

    try:
        detail["rule"] = sonar.get_rule(issue.get("rule") or "")
    except Exception as exc:  # noqa: BLE001 - rule text is optional
        detail["rule"] = {"key": issue.get("rule"), "name": "", "description": ""}
        detail["repo_error"] = f"Could not load the rule description: {exc}"

    repo_path = project.get("repo_path")
    if not repo_path:
        detail["repo_error"] = "No local repository configured for this project."
        return detail

    try:
        workspace = RepoWorkspace(repo_path)
        detail["code_context"] = workspace.context_slice(
            issue["file_path"], issue.get("line"), get_settings().context_radius
        )
    except RepoError as exc:
        detail["repo_error"] = str(exc)

    return detail


# --- the workflow ------------------------------------------------------------


def _plan_payload(issue_id: str) -> dict[str, Any] | None:
    plan = store.latest_plan(issue_id)
    if not plan:
        return None
    payload = dict(plan)
    payload["remediation_plan"] = workflow.plan_steps(plan.get("plan_json"))
    payload.pop("plan_json", None)
    return payload


def _run_payload(plan_id: int | None) -> dict[str, Any] | None:
    if not plan_id:
        return None
    run = store.latest_run(int(plan_id))
    return dict(run) if run else None


def analyze_issue(issue_id: str) -> dict[str, Any]:
    """Steps 4-6: run the analysis agent, then park on the approval interrupt.

    Re-analysing an issue starts a clean run: the old checkpoint is dropped and
    a new fix plan row is written, so previous plans remain as history.
    """
    if not issues_.fetch_one(issue_id):
        raise ServiceError(f"Unknown issue: {issue_id}. It may already be resolved in SonarQube.")

    config = workflow.thread_config(issue_id)
    thread_id = config["configurable"]["thread_id"]
    workflow.get_checkpointer().delete_thread(thread_id)

    try:
        result = workflow.build_graph().invoke({"issue_id": issue_id}, config=config)
    except ValueError as exc:
        raise ServiceError(str(exc)) from exc

    return {
        "issue_id": issue_id,
        "status": result.get("status", "unknown"),
        "analysis": result.get("analysis"),
        "plan": _plan_payload(issue_id),
        "awaiting_approval": bool(result.get("__interrupt__")),
    }


def _resume_payload(issue_id: str, result: dict[str, Any]) -> dict[str, Any]:
    plan = _plan_payload(issue_id)
    return {
        "issue_id": issue_id,
        "status": result.get("status", "unknown"),
        "error": result.get("error"),
        "plan": plan,
        "branch": result.get("branch"),
        "diff": result.get("diff"),
        "commit_sha": result.get("commit_sha"),
        "pr": result.get("pr"),
        "fix": result.get("fix"),
        "run": _run_payload(plan.get("id") if plan else None),
    }


def decide(issue_id: str, approved: bool, feedback: str | None = None) -> dict[str, Any]:
    """Step 7: approve or reject the plan. Resumes the parked run.

    Approving no longer builds automatically - it stops right after the fix
    is generated and parks at `await_build`, waiting for `request_build`.
    """
    config = workflow.thread_config(issue_id)
    graph = workflow.build_graph()

    snapshot = graph.get_state(config)
    if not snapshot.next:
        raise ServiceError(
            "There is no analysis waiting for a decision on this issue. "
            "Run the analysis first."
        )

    if approved and "apply_fix" in snapshot.next:
        # A previous attempt was approved but crashed while applying the fix
        # (e.g. git missing). Continue from that node instead of re-asking.
        result = graph.invoke(None, config=config)
    else:
        result = graph.invoke(
            Command(resume={"approved": approved, "feedback": feedback or ""}),
            config=config,
        )

    return _resume_payload(issue_id, result)


def request_build(issue_id: str) -> dict[str, Any]:
    """Human clicked "Build": actually build the fix that's ready and waiting."""
    config = workflow.thread_config(issue_id)
    graph = workflow.build_graph()
    snapshot = graph.get_state(config)
    if not snapshot.next or "await_build" not in snapshot.next:
        raise ServiceError("Nothing is waiting to be built for this issue.")
    result = graph.invoke(Command(resume=True), config=config)
    return _resume_payload(issue_id, result)


def build_retry(issue_id: str, feedback: str = "") -> dict[str, Any]:
    """Human reviewed a failed build and wants another attempt, with feedback."""
    config = workflow.thread_config(issue_id)
    graph = workflow.build_graph()
    snapshot = graph.get_state(config)
    if not snapshot.next or "await_build_feedback" not in snapshot.next:
        raise ServiceError("No failed build is waiting for a retry on this issue.")
    result = graph.invoke(
        Command(resume={"retry": True, "feedback": feedback or ""}), config=config
    )
    return _resume_payload(issue_id, result)


def build_abandon(issue_id: str) -> dict[str, Any]:
    """Human reviewed a failed build and is done retrying."""
    config = workflow.thread_config(issue_id)
    graph = workflow.build_graph()
    snapshot = graph.get_state(config)
    if not snapshot.next or "await_build_feedback" not in snapshot.next:
        raise ServiceError("No failed build is waiting on this issue.")
    result = graph.invoke(Command(resume={"retry": False}), config=config)
    return _resume_payload(issue_id, result)


def workflow_state(issue_id: str) -> dict[str, Any]:
    """What the UI needs to re-render screen 3 after any page refresh."""
    plan = _plan_payload(issue_id)
    snapshot = workflow.build_graph().get_state(workflow.thread_config(issue_id))
    values = snapshot.values if isinstance(snapshot.values, dict) else {}
    next_nodes = snapshot.next or ()
    return {
        "issue_id": issue_id,
        "status": values.get("status"),
        "awaiting_approval": "await_approval" in next_nodes,
        "awaiting_build": "await_build" in next_nodes,
        "awaiting_build_feedback": "await_build_feedback" in next_nodes,
        "plan": plan,
        "branch": values.get("branch"),
        "diff": values.get("diff"),
        "commit_sha": values.get("commit_sha"),
        "pr": values.get("pr"),
        "fix": values.get("fix"),
        "error": values.get("error"),
        "run": _run_payload(plan.get("id") if plan else None),
    }


def onboard_project(
    sonar_url: str, repo_url: str, branch: str | None = None, repo_path: str | None = None
) -> dict[str, Any]:
    """Paste a SonarQube project URL (or key) and a repo URL, get its issues back.

    The one entry point the UI needs for "I have a new project" - no separate
    sync/clone/fetch steps. Safe to call again with the same inputs: it reuses
    whatever is already bound instead of re-cloning or erroring.

    `repo_path`, when given, is the exact local folder to use: if it is
    already a git clone, it is bound as-is (nothing is cloned); otherwise the
    repo is cloned into that folder. Without it, an existing binding is
    reused, or a fresh clone lands under data/repos/<project>.
    """
    if sonar.is_portfolio_or_application(sonar_url):
        raise ServiceError(
            f"{sonar_url} is a Portfolio/Application page, not a Project. Those "
            "aggregate issues across many unrelated repositories, so they never "
            "line up with the one repo you're fixing. Open the actual Project in "
            "SonarQube (its page title says 'Project', and the URL looks like "
            "/dashboard?id=... or /code?id=...) and paste that link instead."
        )
    key = sonar.parse_project_key(sonar_url)
    if not sonar.server_matches(sonar_url):
        raise ServiceError(
            f"{sonar_url} is not on the configured SonarQube server "
            f"({get_settings().sonar_url}). Point SONAR_URL/SONAR_TOKEN at that "
            "server first (see .env)."
        )

    if not store.get_project(key):
        name = key
        try:
            name = next((p["name"] for p in sonar.list_projects() if p["key"] == key), key)
        except Exception:  # noqa: BLE001 - a nice-to-have, never blocks onboarding
            pass
        store.upsert_projects([{"key": key, "name": name}])

    project = store.get_project(key) or {}
    chosen = (repo_path or "").strip()
    bound_path = chosen or project.get("repo_path")
    path = Path(bound_path) if bound_path else None

    if path and (path / ".git").exists():
        set_repo_path(key, str(path.resolve()))
    elif path and path.is_dir() and any(path.iterdir()):
        raise ServiceError(
            f"{bound_path} exists, is not empty, and is not a git repository. "
            "Point the local folder at an empty or non-existent path to clone into."
        )
    else:
        # Doesn't exist yet, or exists and is empty - either is fine to clone into.
        clone_repo(key, repo_url, branch, dest_path=chosen or None)

    synced = sync_issues(key)
    return {
        "project": store.get_project(key),
        "issues_synced": synced["issues_synced"],
        "issues": list_issues(key),
    }


def create_pull_request(issue_id: str) -> dict[str, Any]:
    """Push the committed fix branch for one issue and open a Bitbucket PR.

    Only reachable once the fix has been committed (`run.status == "applied"`).
    The build result is informational only and does not gate this - a run can
    be 'applied' with a failed, skipped, or unverified build; see
    `run.build_status` / `run.build_output`.
    """
    from . import scm

    plan = store.latest_plan(issue_id)
    if not plan:
        raise ServiceError("Analyze and approve a fix before opening a pull request.")
    run = store.latest_run(int(plan["id"]))
    if not run or run.get("status") != "applied" or not run.get("branch"):
        raise ServiceError("No applied fix is ready to publish for this issue.")
    if run.get("pr_url"):
        return run

    project = store.get_project(plan["project_key"]) or {}
    repo_path = project.get("repo_path")
    readiness = scm.status(repo_path)
    if not readiness["ready"]:
        raise ServiceError(str(readiness["reason"]))

    base_branch = run.get("base_branch") or "main"
    scm.push(repo_path, run["branch"])
    url = scm.open_pull_request(
        repo_path,
        run["branch"],
        base_branch,
        run.get("pr_title") or plan.get("message") or f"SonarFix: {issue_id}",
        run.get("pr_description") or "",
    )
    store.set_run_pr_url(int(run["id"]), url)
    return store.get_run(int(run["id"])) or run


def clone_repo(
    project_key: str, url: str, branch: str | None = None, dest_path: str | None = None
) -> dict[str, Any]:
    """Clone `url` and make it the project's repo.

    `dest_path` lets the caller pick exactly where it lands; without it, the
    clone goes under data/repos/<project>. `url` may be a clone URL or a
    browser page copied from Bitbucket - either normalizes the same way.
    """
    from . import scm
    from .repo import slugify

    if not store.get_project(project_key):
        raise ServiceError(f"Unknown project: {project_key}")
    url = scm.normalize_clone_url(url)
    dest = (
        Path(dest_path).expanduser().resolve()
        if dest_path
        else get_settings().db_path.parent / "repos" / slugify(project_key, limit=80)
    )
    if get_settings().bitbucket_token:
        scm.clone(url, dest, branch)
    else:
        _git("clone", "--single-branch", *(["--branch", branch] if branch else []), url, str(dest))
    return set_repo_path(project_key, str(dest))
