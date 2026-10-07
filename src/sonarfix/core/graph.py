"""The remediation workflow as a LangGraph state machine.

    load_context -> analyze -> await_approval -.-> apply_fix -> await_build -> verify_build -> finalize -.-> END (applied)
                                               '-> (rejected) END                                                      '-> await_build_feedback -.-> apply_fix (retry, loop)
                                                                                                                                                '-> (give up) END

Both `await_approval` and `await_build` call `interrupt()`, so the run parks
in the SQLite checkpointer until a human acts: approve/reject the plan, or
trigger the build explicitly (it never runs automatically). The build result
is informational only - pass, fail, or skipped (e.g. a non-C# change) - and
never blocks `finalize` from committing and handing back a PR; it is just
recorded next to the diff for the reviewer. `await_build_feedback` only
fires when `apply_fix` itself failed (dirty tree, agent crash, no changes),
offering a retry with feedback or giving up.
"""

from __future__ import annotations

import json
import sqlite3
from functools import lru_cache
from typing import Any, Literal, TypedDict

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from . import build, engine, sonar, store
from . import issues as issues_
from .config import get_settings
from .repo import RepoWorkspace, branch_prefix, slugify
from .repo_profile import profile_for

class FixState(TypedDict, total=False):
    # inputs
    issue_id: str
    # resolved context
    project_key: str
    repo_path: str
    file_path: str
    line: int | None
    issue: dict[str, Any]
    rule: dict[str, str]
    code_context: dict[str, Any]
    # analysis
    analysis: dict[str, Any]
    plan_id: int
    # approval
    approved: bool
    feedback: str
    # fix
    base_branch: str
    branch: str
    fix: dict[str, Any]
    diff: str
    changed_files: list[str]
    # build verification
    build: dict[str, Any]
    build_feedback: str
    retry_build: bool
    # output
    pr: dict[str, str]
    commit_sha: str
    run_id: int
    status: str
    error: str


# --- checkpointer ------------------------------------------------------------


@lru_cache(maxsize=1)
def get_checkpointer() -> SqliteSaver:
    """One long-lived saver over the same SQLite file the app already uses."""
    settings = get_settings()
    settings.db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(settings.db_path, check_same_thread=False)
    saver = SqliteSaver(conn)
    saver.setup()
    return saver


def thread_config(issue_id: str) -> dict[str, Any]:
    return {
        "configurable": {"thread_id": f"issue::{issue_id}"},
        "recursion_limit": engine.RECURSION_LIMIT,
    }


# --- prompt assembly ---------------------------------------------------------


def _issue_header(state: FixState) -> str:
    issue = state["issue"]
    rule = state.get("rule") or {}
    line = state.get("line")
    return "\n".join(
        [
            "## SonarQube issue",
            f"- Issue key: {issue.get('id')}",
            f"- Rule: {issue.get('rule')} ({rule.get('name') or 'unknown rule'})",
            f"- Sonar severity: {issue.get('severity')}",
            f"- Sonar type: {issue.get('type')}",
            f"- Message: {issue.get('message')}",
            f"- File: /{state['file_path']}",
            f"- Line: {line if line else 'not reported (file-level issue)'}",
        ]
    )


def _rule_block(state: FixState) -> str:
    description = (state.get("rule") or {}).get("description") or ""
    if not description:
        return ""
    # Rule descriptions are generic boilerplate; a few thousand chars is plenty.
    return f"\n## What the rule says\n\n{description[:4000]}\n"


def _project_block(file_path: str) -> str:
    profile = profile_for(file_path)
    return f"\n## The project this file belongs to\n\n{profile}\n" if profile else ""


def _analysis_task(state: FixState) -> str:
    context = state.get("code_context") or {}
    return "\n".join(
        [
            _issue_header(state),
            _rule_block(state),
            _project_block(state["file_path"]),
            "## Code at the reported location",
            f"Lines {context.get('start_line')}-{context.get('end_line')} of "
            f"/{state['file_path']} ({context.get('total_lines')} lines total):",
            "",
            "```",
            str(context.get("text", "")),
            "```",
            "",
            "Investigate this issue in the repository and produce your analysis "
            "and a remediation plan.",
        ]
    )


def _fix_task(state: FixState) -> str:
    analysis = state.get("analysis") or {}
    plan = analysis.get("remediation_plan") or []
    numbered = "\n".join(f"{i}. {step}" for i, step in enumerate(plan, start=1))
    related = analysis.get("related_files") or []
    feedback = (state.get("feedback") or "").strip()
    prior_build = state.get("build") or {}
    build_feedback = (state.get("build_feedback") or "").strip()

    parts = [
        _issue_header(state),
        _project_block(state["file_path"]),
        "## Approved remediation plan",
        numbered or "(no steps recorded - implement the minimal correct fix)",
        "",
        "## Why this issue matters here",
        str(analysis.get("root_cause", "")),
    ]
    if related:
        parts += ["", "## Files the analysis found relevant"]
        parts += [f"- /{path.lstrip('/')}" for path in related]
    if feedback:
        parts += [
            "",
            "## Reviewer notes on the plan - these override the plan where they conflict",
            feedback,
        ]
    # Only present on a retry: the previous attempt reached a build and it
    # failed, so the agent gets the actual compiler output, not just another
    # blind try at the same plan.
    if prior_build and not prior_build.get("success"):
        parts += [
            "",
            "## The previous attempt did not build - fix this too",
            "```",
            str(prior_build.get("output", ""))[:3000],
            "```",
        ]
    if build_feedback:
        parts += [
            "",
            "## Reviewer notes on the build failure - address these",
            build_feedback,
        ]
    parts += ["", "Implement the approved plan now."]
    return "\n".join(parts)


def _pr_task(state: FixState) -> str:
    issue = state["issue"]
    fix = state.get("fix") or {}
    diff = state.get("diff") or ""
    return "\n".join(
        [
            "Write the commit message and pull request text for this change.",
            "",
            _issue_header(state),
            "",
            "## What the engineer reported",
            str(fix.get("changes_summary", "")),
            "",
            "## Testing suggestions",
            str(fix.get("testing_suggestions", "")),
            "",
            "## The actual diff",
            "```diff",
            diff[:60_000],
            "```",
            "",
            "Describe only what the diff actually does. Reference Sonar issue "
            f"{issue.get('id')} and rule {issue.get('rule')}. Do not claim tests "
            "were run - they were not.",
        ]
    )


# --- nodes -------------------------------------------------------------------


def load_context(state: FixState) -> dict[str, Any]:
    issue = issues_.fetch_one(state["issue_id"])
    if not issue:
        raise ValueError(
            f"Unknown issue: {state['issue_id']}. It may already be resolved in SonarQube."
        )

    project = store.get_project(issue["project_key"])
    if not project or not project.get("repo_path"):
        raise ValueError(
            f"No local repository configured for project {issue['project_key']}. "
            "Set the repo path on the Projects screen."
        )

    workspace = RepoWorkspace(project["repo_path"])
    file_path = issue["file_path"]
    if not workspace.exists(file_path):
        raise ValueError(
            f"{file_path} is not present in {project['repo_path']}. "
            "Is the clone on the branch Sonar analysed?"
        )

    try:
        rule = sonar.get_rule(issue.get("rule") or "")
    except Exception:  # noqa: BLE001 - the rule text is a nice-to-have
        rule = {"key": issue.get("rule") or "", "name": "", "description": ""}

    return {
        "project_key": issue["project_key"],
        "repo_path": project["repo_path"],
        "file_path": file_path,
        "line": issue.get("line"),
        "issue": issue,
        "rule": rule,
        "code_context": workspace.context_slice(
            file_path, issue.get("line"), get_settings().context_radius
        ),
        "status": "analyzing",
    }


def analyze(state: FixState) -> dict[str, Any]:
    analysis = engine.run_analysis(state["repo_path"], _analysis_task(state))
    plan_id = store.create_plan(state["issue"], analysis, get_settings().model)
    return {"analysis": analysis, "plan_id": plan_id, "status": "awaiting_approval"}


def await_approval(state: FixState) -> dict[str, Any]:
    analysis = state.get("analysis") or {}
    decision = interrupt(
        {
            "kind": "fix_plan_approval",
            "issue_id": state["issue_id"],
            "plan_id": state.get("plan_id"),
            "file_path": state.get("file_path"),
            "confidence": analysis.get("confidence"),
            "remediation_plan": analysis.get("remediation_plan", []),
        }
    )

    # Accept either the full payload or a bare boolean as the resume value.
    if isinstance(decision, dict):
        approved = bool(decision.get("approved"))
        feedback = str(decision.get("feedback") or "").strip()
    else:
        approved = bool(decision)
        feedback = ""

    store.set_plan_status(
        int(state["plan_id"]),
        "approved" if approved else "rejected",
        feedback or None,
    )
    return {
        "approved": approved,
        "feedback": feedback,
        "status": "applying" if approved else "rejected",
    }


def route_approval(state: FixState) -> Literal["apply_fix", "__end__"]:
    return "apply_fix" if state.get("approved") else END


def apply_fix(state: FixState) -> dict[str, Any]:
    workspace = RepoWorkspace(state["repo_path"])
    base_branch = workspace.current_branch()

    if workspace.is_dirty():
        return {
            "status": "failed",
            "error": (
                f"{state['repo_path']} has uncommitted changes. SonarFix commits "
                "the fix for you, so it needs a clean working tree - commit or "
                "stash your work, then retry."
            ),
            "base_branch": base_branch,
        }

    rule_slug = slugify((state["issue"].get("rule") or "issue").replace(":", "-"))
    branch = workspace.start_branch(f"{branch_prefix()}/{rule_slug}-{state['issue_id'][:8]}")

    try:
        fix = engine.run_fix(state["repo_path"], _fix_task(state))
    except Exception as exc:  # noqa: BLE001 - surface it, then clean up
        workspace.abandon(base_branch, branch)
        return {
            "base_branch": base_branch,
            "branch": branch,
            "status": "failed",
            "error": f"The fix agent failed: {exc}",
        }

    workspace.stage_all()
    diff = workspace.staged_diff()
    changed = workspace.staged_files()

    if not diff.strip():
        workspace.abandon(base_branch, branch)
        return {
            "base_branch": base_branch,
            "branch": branch,
            "fix": fix,
            "diff": "",
            "changed_files": [],
            "status": "failed",
            "error": (
                "The agent made no file changes. Its report: "
                + str(fix.get("changes_summary", ""))[:800]
            ),
        }

    return {
        "base_branch": base_branch,
        "branch": branch,
        "fix": fix,
        "diff": diff,
        "changed_files": changed,
        "status": "fix_generated",
    }


def await_build(state: FixState) -> dict[str, Any]:
    """Pause so a human explicitly triggers the build - it never runs on its
    own right after the fix is generated. Skipped when apply_fix itself
    already failed; there's nothing staged to build."""
    if state.get("status") != "fix_generated":
        return {}
    interrupt(
        {
            "kind": "await_build",
            "issue_id": state["issue_id"],
            "branch": state.get("branch"),
            "diff": state.get("diff"),
        }
    )
    return {}  # the resume value itself doesn't matter - resuming means "build now"


def verify_build(state: FixState) -> dict[str, Any]:
    """Build the blast radius of the fix, purely for information.

    The result (passed/failed/skipped - e.g. a non-C# change, or no build
    tool on this machine) is recorded alongside the diff but never blocks the
    fix from being committed and offered as a PR; it is the reviewer's call.
    `build.verify` already folds in a note when some changed files are in a
    language that is not built yet.
    """
    if state.get("status") != "fix_generated":
        return {}  # apply_fix already failed - nothing staged to build

    result = build.verify(state["repo_path"], state.get("changed_files") or [])
    return {"build": result, "status": "build_checked"}


def finalize(state: FixState) -> dict[str, Any]:
    plan_id = int(state["plan_id"])
    build_result = state.get("build") or {}

    if state.get("status") == "failed":
        run_id = store.create_run(
            plan_id,
            {
                "branch": state.get("branch"),
                "base_branch": state.get("base_branch"),
                "diff": state.get("diff"),
                "changes_summary": (state.get("fix") or {}).get("changes_summary"),
                "status": "failed",
                "error": state.get("error"),
                "build_status": build_result.get("status"),
                "build_output": build_result.get("output"),
            },
        )
        return {"run_id": run_id}

    pr = engine.run_pr_text(state["repo_path"], _pr_task(state))

    workspace = RepoWorkspace(state["repo_path"])
    commit_sha = workspace.commit(pr["commit_message"])

    fix = state.get("fix") or {}
    run_id = store.create_run(
        plan_id,
        {
            "branch": state.get("branch"),
            "base_branch": state.get("base_branch"),
            "diff": state.get("diff"),
            "changes_summary": fix.get("changes_summary"),
            "commit_message": pr["commit_message"],
            "pr_title": pr["pr_title"],
            "pr_description": f"# {pr['pr_title']}\n\n{pr['pr_description']}",
            "testing_suggestions": fix.get("testing_suggestions"),
            "build_status": build_result.get("status"),
            "build_output": build_result.get("output"),
            "status": "applied",
        },
    )
    return {"pr": pr, "commit_sha": commit_sha, "run_id": run_id, "status": "applied"}


def route_after_finalize(state: FixState) -> Literal["await_build_feedback", "__end__"]:
    return "await_build_feedback" if state.get("status") == "failed" else END


def await_build_feedback(state: FixState) -> dict[str, Any]:
    """The attempt failed - a bad build, or apply_fix itself (crash, no
    changes, dirty tree). The error is already visible (finalize recorded it
    in state.error/state.build and in a fix_run row). Park here until a human
    either retries with feedback or gives up."""
    decision = interrupt(
        {
            "kind": "build_failed",
            "issue_id": state["issue_id"],
            "error": state.get("error"),
            "build": state.get("build"),
        }
    )
    if isinstance(decision, dict):
        retry = bool(decision.get("retry"))
        feedback = str(decision.get("feedback") or "").strip()
    else:
        retry = bool(decision)
        feedback = ""
    return {
        "retry_build": retry,
        "build_feedback": feedback,
        "status": "retrying" if retry else "failed",
    }


def route_after_build_feedback(state: FixState) -> Literal["apply_fix", "__end__"]:
    return "apply_fix" if state.get("retry_build") else END


# --- graph -------------------------------------------------------------------


@lru_cache(maxsize=1)
def build_graph() -> Any:
    builder = StateGraph(FixState)
    builder.add_node("load_context", load_context)
    builder.add_node("analyze", analyze)
    builder.add_node("await_approval", await_approval)
    builder.add_node("apply_fix", apply_fix)
    builder.add_node("await_build", await_build)
    builder.add_node("verify_build", verify_build)
    builder.add_node("finalize", finalize)
    builder.add_node("await_build_feedback", await_build_feedback)

    builder.add_edge(START, "load_context")
    builder.add_edge("load_context", "analyze")
    builder.add_edge("analyze", "await_approval")
    builder.add_conditional_edges("await_approval", route_approval)
    builder.add_edge("apply_fix", "await_build")
    builder.add_edge("await_build", "verify_build")
    builder.add_edge("verify_build", "finalize")
    builder.add_conditional_edges("finalize", route_after_finalize)
    builder.add_conditional_edges("await_build_feedback", route_after_build_feedback)

    return builder.compile(checkpointer=get_checkpointer())


def plan_steps(plan_json: str | None) -> list[str]:
    """fix_plans.plan_json -> list of steps, tolerant of bad rows."""
    if not plan_json:
        return []
    try:
        value = json.loads(plan_json)
    except json.JSONDecodeError:
        return []
    return [str(step) for step in value] if isinstance(value, list) else []
