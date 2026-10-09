"""FastAPI surface. A thin HTTP skin over core.service - no logic lives here."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import BackgroundTasks, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from ..core import batch, engine, planner, report, scm, service, store
from ..core.config import CLAUDE_MODELS, ConfigError, get_settings, set_model_override
from ..core.repo import RepoError
from ..core.service import ServiceError
from ..core.sonar import SonarError
from ..core.batch import BatchError
from ..core.scm import ScmError


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    store.init_db()
    yield


app = FastAPI(
    title=" SonarFix",
    version="0.1.0",
    description="AI-assisted remediation of SonarQube issues.",
    lifespan=lifespan,
)

# The React frontend runs on its own dev server / origin.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Anything in here is the user's to fix, so report it as a 400 with the message.
_USER_ERRORS = (
    ServiceError, ConfigError, SonarError, RepoError, ValueError, BatchError, ScmError,
)


def _guard(fn, *args: Any, **kwargs: Any) -> Any:
    try:
        return fn(*args, **kwargs)
    except _USER_ERRORS as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


# --- request bodies ----------------------------------------------------------


class RepoPathBody(BaseModel):
    repo_path: str = Field(description="Absolute path to a local git clone.")


class CloneBody(BaseModel):
    url: str = Field(description="HTTPS or SSH clone URL, e.g. a Bitbucket repository.")
    branch: str | None = Field(default=None, description="Branch Sonar analysed.")
    dest_path: str | None = Field(
        default=None, description="Local folder to clone into; defaults to data/repos/<project>."
    )


class OnboardBody(BaseModel):
    sonar_url: str = Field(description="SonarQube project dashboard URL, or a bare project key.")
    repo_url: str = Field(description="Repository clone URL, e.g. a Bitbucket Server repo.")
    branch: str | None = Field(default=None, description="Branch Sonar analysed, if not the default.")
    repo_path: str | None = Field(
        default=None,
        description="Local folder to use: bound as-is if it's already a clone, else cloned into.",
    )


class BatchBody(BaseModel):
    mechanical: list[str] = Field(default_factory=list, description="Mechanical group ids.")
    ai: list[str] = Field(default_factory=list, description="AI group ids.")
    approve_ai: bool = Field(
        default=False, description="Must be true when `ai` is non-empty: a human approved AI use."
    )
    notes: str = ""


class DecisionBody(BaseModel):
    feedback: str | None = Field(
        default=None,
        description="Optional reviewer notes; they override the plan where they conflict.",
    )


class BuildRetryBody(BaseModel):
    feedback: str | None = Field(
        default=None,
        description="Instructions for the next attempt; the previous build's output is sent too.",
    )


class ModelBody(BaseModel):
    model: str = Field(description="Claude model id to use for new AI sessions.")


# --- routes ------------------------------------------------------------------


@app.get("/health")
def health() -> dict[str, str]:
    """Also reports which engine, provider and MCP wiring is active."""
    try:
        wiring = engine.describe()
    except Exception as exc:  # noqa: BLE001 - health must never 500
        wiring = f"misconfigured: {exc}"
    return {"status": "ok", "engine": wiring}


@app.get("/settings/model")
def get_model_settings() -> dict[str, Any]:
    """The active Claude model and the choices the UI's dropdown offers."""
    settings = get_settings()
    return {
        "model": settings.model,
        "provider": settings.provider,
        "choices": [{"id": model_id, "label": label} for model_id, label in CLAUDE_MODELS.items()],
    }


@app.post("/settings/model")
def post_model_settings(body: ModelBody) -> dict[str, Any]:
    """Switch which Claude model new AI sessions use, from now on."""
    return _guard(set_model_override, body.model) or get_model_settings()


@app.get("/projects")
def get_projects() -> list[dict[str, Any]]:
    return service.list_projects()


@app.post("/projects/sync")
def post_projects_sync() -> list[dict[str, Any]]:
    """Step 1: refresh the project list from SonarQube."""
    return _guard(service.sync_projects)


@app.post("/onboard")
def post_onboard(body: OnboardBody) -> dict[str, Any]:
    """Paste a SonarQube project link and a repo link, get its issues back."""
    return _guard(service.onboard_project, body.sonar_url, body.repo_url, body.branch, body.repo_path)


@app.put("/projects/{project_key}/repo")
def put_repo_path(project_key: str, body: RepoPathBody) -> dict[str, Any]:
    return _guard(service.set_repo_path, project_key, body.repo_path)


@app.post("/projects/{project_key}/auto-clone")
def post_auto_clone(project_key: str) -> dict[str, Any]:
    """Discover the repo SonarQube has bound to this project and clone it."""
    repo_path = _guard(service.ensure_repo, project_key)
    project = store.get_project(project_key)
    return project or {"key": project_key, "repo_path": repo_path}


@app.get("/projects/{project_key}/report")
def get_report(project_key: str) -> HTMLResponse:
    """A single downloadable HTML file: every issue and its remediation plan."""
    html_text = _guard(report.generate_html_report, project_key)
    return HTMLResponse(
        content=html_text,
        headers={
            "Content-Disposition": f'attachment; filename="sonarfix-{project_key}.html"'
        },
    )


@app.post("/projects/{project_key}/sync-issues")
def post_issues_sync(project_key: str) -> dict[str, Any]:
    """Step 2: fetch the project's open issues."""
    return _guard(service.sync_issues, project_key)


@app.get("/projects/{project_key}/issues")
def get_issues(
    project_key: str, severity: str | None = None, type: str | None = None
) -> list[dict[str, Any]]:
    return _guard(service.list_issues, project_key, severity, type)


@app.get("/projects/{project_key}/facets")
def get_facets(project_key: str) -> dict[str, list[str]]:
    return _guard(service.issue_facets, project_key)


@app.get("/issues/{issue_id}")
def get_issue(issue_id: str) -> dict[str, Any]:
    """Step 3: issue detail, rule description and the code at the reported line."""
    return _guard(service.issue_detail, issue_id)


@app.post("/issues/{issue_id}/analyze")
def post_analyze(issue_id: str) -> dict[str, Any]:
    """Steps 4-6: the analysis agent explores the repo and plans the fix."""
    return _guard(service.analyze_issue, issue_id)


@app.get("/issues/{issue_id}/state")
def get_workflow_state(issue_id: str) -> dict[str, Any]:
    return _guard(service.workflow_state, issue_id)


@app.post("/issues/{issue_id}/approve")
def post_approve(issue_id: str, body: DecisionBody | None = None) -> dict[str, Any]:
    """Step 7 approve, then steps 8-10: fix, commit message, PR description."""
    feedback = body.feedback if body else None
    return _guard(service.decide, issue_id, True, feedback)


@app.post("/issues/{issue_id}/reject")
def post_reject(issue_id: str, body: DecisionBody | None = None) -> dict[str, Any]:
    feedback = body.feedback if body else None
    return _guard(service.decide, issue_id, False, feedback)


@app.post("/issues/{issue_id}/build")
def post_build(issue_id: str) -> dict[str, Any]:
    """Human-triggered: build the fix that's generated and waiting."""
    return _guard(service.request_build, issue_id)


@app.post("/issues/{issue_id}/build/retry")
def post_build_retry(issue_id: str, body: BuildRetryBody | None = None) -> dict[str, Any]:
    """A build failed; try again, optionally with reviewer feedback."""
    feedback = body.feedback if body else None
    return _guard(service.build_retry, issue_id, feedback or "")


@app.post("/issues/{issue_id}/build/abandon")
def post_build_abandon(issue_id: str) -> dict[str, Any]:
    """A build failed and the reviewer is done retrying."""
    return _guard(service.build_abandon, issue_id)


@app.post("/issues/{issue_id}/pull-request")
def post_issue_pull_request(issue_id: str) -> dict[str, Any]:
    """Push the fix branch and open a Bitbucket pull request for one issue."""
    return _guard(service.create_pull_request, issue_id)


# --- remediation plan and batches ---------------------------------------------


@app.post("/projects/{project_key}/clone")
def post_clone(project_key: str, body: CloneBody) -> dict[str, Any]:
    """Clone a repository (Bitbucket token used if set) and attach it to the project."""
    return _guard(service.clone_repo, project_key, body.url, body.branch, body.dest_path)


@app.get("/projects/{project_key}/plan")
def get_plan(project_key: str) -> dict[str, Any]:
    """Group issues and route each to mechanical / AI / skip. No LLM calls."""
    return _guard(planner.build_plan, project_key)


@app.get("/projects/{project_key}/scm")
def get_scm_status(project_key: str) -> dict[str, Any]:
    project = store.get_project(project_key) or {}
    return scm.status(project.get("repo_path"))


@app.post("/projects/{project_key}/batches")
def post_batch(project_key: str, body: BatchBody, background: BackgroundTasks) -> dict[str, Any]:
    """Queue the approved groups; the batch runs in the background."""
    batch_id = _guard(
        batch.create, project_key, body.mechanical, body.ai, body.approve_ai, body.notes
    )
    background.add_task(batch.run, batch_id)
    return store.get_batch(batch_id) or {"id": batch_id}


@app.get("/projects/{project_key}/batches")
def get_batches(project_key: str) -> list[dict[str, Any]]:
    return store.list_batches(project_key)


@app.get("/batches/{batch_id}")
def get_batch(batch_id: int) -> dict[str, Any]:
    found = store.get_batch(batch_id)
    if not found:
        raise HTTPException(status_code=404, detail=f"Unknown batch: {batch_id}")
    return found


@app.post("/batches/{batch_id}/build")
def post_batch_build(batch_id: int) -> dict[str, Any]:
    """Human-triggered: build the batch's blast radius before it can get a PR."""
    return _guard(batch.request_build, batch_id)


@app.post("/batches/{batch_id}/retry")
def post_batch_retry(
    batch_id: int, background: BackgroundTasks, body: BuildRetryBody | None = None
) -> dict[str, Any]:
    """A batch's build failed; start a fresh attempt with feedback."""
    feedback = body.feedback if body else None
    new_id = _guard(batch.retry_with_feedback, batch_id, feedback or "")
    background.add_task(batch.run, new_id)
    return store.get_batch(new_id) or {"id": new_id}


@app.post("/batches/{batch_id}/abandon")
def post_batch_abandon(batch_id: int) -> dict[str, Any]:
    """A batch's build failed and the reviewer is done retrying."""
    return _guard(batch.abandon, batch_id)


@app.post("/batches/{batch_id}/pull-request")
def post_pull_request(batch_id: int) -> dict[str, Any]:
    """Push the batch branch and open a Bitbucket pull request (human-triggered)."""
    return _guard(batch.create_pull_request, batch_id)
