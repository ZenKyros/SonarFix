"""Streamlit front end. Three screens, routed by st.session_state["screen"].

Talks to the FastAPI backend over HTTP so the two halves stay independent.
Agent runs are slow, so the client timeout is generous.
"""

from __future__ import annotations

import os
from typing import Any

import httpx
import streamlit as st

API_URL = os.environ.get("SONARFIX_API_URL", "http://127.0.0.1:8000").rstrip("/")
# An agent exploring a repository can legitimately take several minutes.
TIMEOUT = httpx.Timeout(900.0, connect=10.0)

SEVERITY_ICON = {
    "BLOCKER": "🟥",
    "CRITICAL": "🟧",
    "MAJOR": "🟨",
    "MINOR": "🟦",
    "INFO": "⬜",
}


# --- API client --------------------------------------------------------------


def api(method: str, path: str, **kwargs: Any) -> Any:
    try:
        response = httpx.request(
            method, f"{API_URL}{path}", timeout=TIMEOUT, **kwargs
        )
    except httpx.HTTPError as exc:
        st.error(f"Cannot reach the SonarFix API at {API_URL}: {exc}")
        st.stop()

    if response.status_code == 400:
        st.error(response.json().get("detail", response.text))
        st.stop()
    if response.status_code >= 400:
        st.error(f"{method} {path} failed ({response.status_code}): {response.text[:500]}")
        st.stop()
    return response.json()


def goto(screen: str, **state: Any) -> None:
    st.session_state["screen"] = screen
    st.session_state.update(state)
    st.rerun()


# --- screen 1: projects ------------------------------------------------------


def screen_projects() -> None:
    st.header("Projects")
    st.caption(
        "Pick the SonarQube project to work on and point SonarFix at a local clone "
        "of its repository."
    )

    if st.button("Sync projects from SonarQube", type="primary"):
        projects = api("POST", "/projects/sync")
        st.success(f"Loaded {len(projects)} projects.")

    projects = api("GET", "/projects")
    if not projects:
        st.info("No projects yet. Sync from SonarQube to get started.")
        return

    labels = {f"{p['name']}  ({p['key']})": p for p in projects}
    choice = st.selectbox("Project", list(labels))
    project = labels[choice]

    repo_path = st.text_input(
        "Local git clone",
        value=project.get("repo_path") or "",
        placeholder=r"C:\work\my-service",
        help="SonarFix reads and edits this working copy. It must be a git repository.",
    )

    col_save, col_sync = st.columns(2)
    with col_save:
        if st.button("Save repository path", disabled=not repo_path.strip()):
            api(
                "PUT",
                f"/projects/{project['key']}/repo",
                json={"repo_path": repo_path.strip()},
            )
            st.success("Saved.")
            st.rerun()
    with col_sync:
        if st.button("Fetch issues", type="primary"):
            with st.spinner("Fetching issues from SonarQube..."):
                result = api("POST", f"/projects/{project['key']}/sync-issues")
            st.success(f"Synced {result['issues_synced']} open issues.")
            goto("issues", project_key=project["key"], project_name=project["name"])

    if project.get("last_synced_at"):
        st.caption(f"Issues last synced: {project['last_synced_at']}")
    if project.get("repo_path"):
        if st.button("Browse issues"):
            goto("issues", project_key=project["key"], project_name=project["name"])


# --- screen 2: issue list ----------------------------------------------------


def screen_issues() -> None:
    project_key = st.session_state.get("project_key")
    if not project_key:
        goto("projects")

    st.header(st.session_state.get("project_name") or project_key)
    if st.button("← Projects"):
        goto("projects")

    facets = api("GET", f"/projects/{project_key}/facets")
    col_sev, col_type = st.columns(2)
    with col_sev:
        severity = st.selectbox("Severity", ["All", *facets["severities"]])
    with col_type:
        issue_type = st.selectbox("Type", ["All", *facets["types"]])

    params: dict[str, str] = {}
    if severity != "All":
        params["severity"] = severity
    if issue_type != "All":
        params["type"] = issue_type

    issues = api("GET", f"/projects/{project_key}/issues", params=params)
    if not issues:
        st.info("No issues match these filters.")
        return

    st.caption(f"{len(issues)} issues, worst first.")
    for issue in issues[:200]:
        icon = SEVERITY_ICON.get(issue["severity"], "⬜")
        line = f":{issue['line']}" if issue.get("line") else ""
        with st.container(border=True):
            st.markdown(
                f"{icon} **{issue['severity']}** · `{issue['rule']}`  \n"
                f"{issue['message']}  \n"
                f"`{issue['file_path']}{line}`"
            )
            if st.button("Open", key=f"open-{issue['id']}"):
                goto("detail", issue_id=issue["id"])

    if len(issues) > 200:
        st.caption("Showing the first 200. Narrow the filters to see the rest.")


# --- screen 3: issue detail --------------------------------------------------


def _render_issue(detail: dict[str, Any]) -> None:
    issue = detail["issue"]
    st.subheader(issue["message"])
    st.markdown(
        f"{SEVERITY_ICON.get(issue['severity'], '⬜')} **{issue['severity']}** · "
        f"{issue['type']} · `{issue['rule']}` · `{issue['file_path']}`"
        + (f" line {issue['line']}" if issue.get("line") else "")
    )

    rule = detail.get("rule") or {}
    if rule.get("description"):
        with st.expander(f"Why Sonar reported this — {rule.get('name') or rule.get('key')}"):
            st.text(rule["description"][:6000])

    if detail.get("repo_error"):
        st.warning(detail["repo_error"])

    context = detail.get("code_context")
    if context:
        st.caption(
            f"{issue['file_path']} lines {context['start_line']}–{context['end_line']} "
            f"of {context['total_lines']}"
        )
        st.code(context["text"], language=None)


def _render_plan(state: dict[str, Any], issue_id: str) -> None:
    plan = state.get("plan")
    if not plan:
        return

    st.divider()
    st.subheader("AI analysis")

    confidence = plan.get("confidence")
    if confidence is not None:
        st.progress(
            min(max(float(confidence), 0.0), 1.0),
            text=f"Confidence {float(confidence):.0%}",
        )

    st.markdown("**What this means**")
    st.write(plan.get("explanation") or "—")
    st.markdown("**Root cause**")
    st.write(plan.get("root_cause") or "—")
    st.markdown("**Impact**")
    st.write(plan.get("impact") or "—")

    steps = plan.get("remediation_plan") or []
    if steps:
        st.markdown("**Remediation plan**")
        for index, step in enumerate(steps, start=1):
            st.markdown(f"{index}. {step}")

    if plan.get("testing_notes"):
        st.markdown("**How to verify**")
        st.write(plan["testing_notes"])

    if state.get("awaiting_approval"):
        st.divider()
        st.markdown("**Approve this plan?** Nothing is written to the repository until you do.")
        feedback = st.text_area(
            "Reviewer notes (optional)",
            placeholder="Anything here overrides the plan where they conflict.",
            key=f"feedback-{issue_id}",
        )
        col_yes, col_no = st.columns(2)
        with col_yes:
            if st.button("Approve and generate fix", type="primary"):
                with st.spinner("The fix agent is editing the repository..."):
                    api(
                        "POST",
                        f"/issues/{issue_id}/approve",
                        json={"feedback": feedback or None},
                    )
                st.rerun()
        with col_no:
            if st.button("Reject plan"):
                api(
                    "POST",
                    f"/issues/{issue_id}/reject",
                    json={"feedback": feedback or None},
                )
                st.rerun()
    elif plan.get("status") == "rejected":
        st.info("You rejected this plan. Re-run the analysis to try again.")


def _render_fix(state: dict[str, Any]) -> None:
    status = state.get("status")
    if status not in {"applied", "failed", "fix_generated"}:
        return

    st.divider()
    st.subheader("Fix")

    if status == "failed":
        st.error(state.get("error") or "The fix could not be generated.")
        return

    fix = state.get("fix") or {}
    if state.get("branch"):
        st.success(f"Changes committed to branch `{state['branch']}`")
    if state.get("commit_sha"):
        st.caption(f"Commit {state['commit_sha']}")

    if fix.get("changes_summary"):
        st.markdown("**What changed**")
        st.write(fix["changes_summary"])

    if state.get("diff"):
        st.markdown("**Diff**")
        st.code(state["diff"], language="diff")

    pr = state.get("pr") or {}
    run = state.get("run") or {}
    commit_message = pr.get("commit_message") or run.get("commit_message")
    pr_description = run.get("pr_description")
    if pr and not pr_description:
        pr_description = f"# {pr.get('pr_title', '')}\n\n{pr.get('pr_description', '')}"

    if commit_message:
        st.markdown("**Commit message**")
        st.code(commit_message, language=None)
    if pr_description:
        st.markdown("**PR description**")
        st.code(pr_description, language="markdown")

    testing = fix.get("testing_suggestions") or run.get("testing_suggestions")
    if testing:
        st.markdown("**Testing suggestions**")
        st.write(testing)

    if state.get("branch"):
        st.caption(
            "Push it yourself when you are happy: "
            f"`git push -u origin {state['branch']}`"
        )


def screen_detail() -> None:
    issue_id = st.session_state.get("issue_id")
    if not issue_id:
        goto("issues")

    if st.button("← Issues"):
        goto("issues")

    detail = api("GET", f"/issues/{issue_id}")
    _render_issue(detail)

    state = api("GET", f"/issues/{issue_id}/state")
    has_plan = bool(state.get("plan"))

    st.divider()
    label = "Re-run analysis" if has_plan else "Analyze with AI"
    if st.button(label, type="secondary" if has_plan else "primary"):
        with st.spinner("The analysis agent is reading the repository..."):
            api("POST", f"/issues/{issue_id}/analyze")
        st.rerun()

    _render_plan(state, issue_id)
    _render_fix(state)


# --- entry point -------------------------------------------------------------

SCREENS = {
    "projects": screen_projects,
    "issues": screen_issues,
    "detail": screen_detail,
}


def main() -> None:
    st.set_page_config(page_title=" SonarFix", page_icon="🛠", layout="wide")
    st.title(" SonarFix")
    st.session_state.setdefault("screen", "projects")
    SCREENS[st.session_state["screen"]]()

    wiring = api("GET", "/health").get("engine")
    if wiring:
        st.sidebar.caption("Active wiring")
        st.sidebar.code(wiring, language=None)


main()
