"""Use cases. The API and the UI both go through this module, never deeper."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from langgraph.types import Command

from . import graph as workflow
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
    if not store.get_project(project_key):
        raise ServiceError(f"Unknown project: {project_key}. Sync projects first.")
    issues = sonar.list_issues(project_key)
    count = store.replace_issues(project_key, issues)
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
    return store.list_issues(project_key, severity, issue_type)


def issue_facets(project_key: str) -> dict[str, list[str]]:
    return store.issue_facets(project_key)


def issue_detail(issue_id: str) -> dict[str, Any]:
    """Issue row, rule description and the code around the reported line."""
    issue = store.get_issue(issue_id)
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
    if not store.get_issue(issue_id):
        raise ServiceError(f"Unknown issue: {issue_id}. Sync the project first.")

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


def decide(issue_id: str, approved: bool, feedback: str | None = None) -> dict[str, Any]:
    """Step 7, then 8-10 when approved. Resumes the parked run."""
    config = workflow.thread_config(issue_id)
    graph = workflow.build_graph()

    snapshot = graph.get_state(config)
    if not snapshot.next:
        raise ServiceError(
            "There is no analysis waiting for a decision on this issue. "
            "Run the analysis first."
        )

    result = graph.invoke(
        Command(resume={"approved": approved, "feedback": feedback or ""}),
        config=config,
    )

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


def workflow_state(issue_id: str) -> dict[str, Any]:
    """What the UI needs to re-render screen 3 after any page refresh."""
    plan = _plan_payload(issue_id)
    snapshot = workflow.build_graph().get_state(workflow.thread_config(issue_id))
    values = snapshot.values if isinstance(snapshot.values, dict) else {}
    return {
        "issue_id": issue_id,
        "status": values.get("status"),
        "awaiting_approval": bool(snapshot.next and "await_approval" in snapshot.next),
        "plan": plan,
        "branch": values.get("branch"),
        "diff": values.get("diff"),
        "commit_sha": values.get("commit_sha"),
        "pr": values.get("pr"),
        "fix": values.get("fix"),
        "error": values.get("error"),
        "run": _run_payload(plan.get("id") if plan else None),
    }


def clone_repo(project_key: str, url: str, branch: str | None = None) -> dict[str, Any]:
    """Clone `url` under data/repos/<project> and make it the project's repo."""
    from . import scm
    from .repo import slugify

    if not store.get_project(project_key):
        raise ServiceError(f"Unknown project: {project_key}")
    dest = get_settings().db_path.parent / "repos" / slugify(project_key, limit=80)
    if get_settings().bitbucket_token:
        scm.clone(url, dest, branch)
    else:
        _git("clone", "--single-branch", *(["--branch", branch] if branch else []), url, str(dest))
    return set_repo_path(project_key, str(dest))
