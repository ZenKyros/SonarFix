"""Run an approved remediation batch on one branch, then open one pull request.

    mechanical groups  recipes, one commit, zero tokens
    ai groups          one AI session per group (not per issue), one commit each,
                       and only groups a human explicitly approved
"""

from __future__ import annotations

import ast
import traceback
from collections import defaultdict
from typing import Any

from . import build, engine, scm, sonar, store
from .config import get_settings
from .planner import Group, Occurrence, SourceFile, build_groups, classify
from .recipes import target_from
from .repo import RepoWorkspace, branch_prefix
from .repo_profile import profile_for


class BatchError(RuntimeError):
    """The selection is invalid or the batch cannot run."""


# --- selection ---------------------------------------------------------------


def create(
    project_key: str,
    mechanical: list[str],
    ai: list[str],
    approve_ai: bool,
    notes: str = "",
) -> int:
    if not mechanical and not ai:
        raise BatchError("Select at least one group to fix.")
    if ai and not approve_ai:
        raise BatchError("AI groups need explicit approval before they run.")

    occurrences, workspace = classify(project_key)
    groups = {g.id: g for g in build_groups(occurrences)}
    for group_id in mechanical:
        if groups.get(group_id) is None or groups[group_id].bucket != "mechanical":
            raise BatchError(f"{group_id} is not a mechanical group in the current plan.")
    for group_id in ai:
        if groups.get(group_id) is None or groups[group_id].bucket != "ai":
            raise BatchError(f"{group_id} is not an AI group in the current plan.")
    if workspace.is_dirty():
        raise BatchError(
            f"{workspace.path} has uncommitted changes. Commit or stash them first - "
            "SonarFix commits its fixes on a fresh branch."
        )

    return store.create_batch(
        project_key,
        {"mechanical": mechanical, "ai": ai, "notes": notes.strip()},
    )


# --- execution ---------------------------------------------------------------


def _step(steps: list[dict[str, Any]], batch_id: int, **step: Any) -> dict[str, Any]:
    steps.append(step)
    store.update_batch(batch_id, steps=steps)
    return step


def _save(steps: list[dict[str, Any]], batch_id: int) -> None:
    store.update_batch(batch_id, steps=steps)


def _apply_mechanical(
    workspace: RepoWorkspace, groups: list[Group]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Apply every selected recipe; returns (applied, failed) occurrence dicts."""
    by_file: dict[str, list[Occurrence]] = defaultdict(list)
    for group in groups:
        for occ in group.occurrences:
            if not occ.duplicate_of:
                by_file[occ.issue["file_path"]].append(occ)

    applied, failed = [], []
    for file_path, occs in by_file.items():
        src = SourceFile.load(workspace.resolve(file_path))
        lines = list(src.lines)
        # Bottom-up, right-to-left: earlier positions stay valid after each edit.
        occs.sort(
            key=lambda o: (
                target_from(o.issue, o.raw).start_line,
                target_from(o.issue, o.raw).start_col or 0,
            ),
            reverse=True,
        )
        done: list[Occurrence] = []
        for occ in occs:
            new = occ.recipe.apply(lines, target_from(occ.issue, occ.raw)) if occ.recipe else None
            if new is None:
                failed.append({**occ.as_dict(), "reason": "Code no longer matches the recipe."})
            else:
                lines = new
                done.append(occ)

        if done and file_path.endswith(".py"):
            try:
                ast.parse("".join(lines))
            except SyntaxError as exc:
                failed += [
                    {**o.as_dict(), "reason": f"Python syntax check failed: {exc}"} for o in done
                ]
                continue
        if done:
            src.save(lines)
            applied += [{**o.as_dict(), "recipe": o.recipe.title} for o in done]
    order = lambda item: (item["file"] or "", item["line"] or 0)  # noqa: E731
    return sorted(applied, key=order), sorted(failed, key=order)


def _ai_task(group: Group, rule: dict[str, str], notes: str, limit: int) -> str:
    primary = [o for o in group.occurrences if not o.duplicate_of][:limit]
    parts = [
        "## Approved work",
        f"A reviewer approved fixing every occurrence below of SonarQube rule "
        f"{group.rule} ({rule.get('name') or 'rule name unavailable'}).",
        "Fix each one with the smallest correct change. Use the same approach for "
        "every occurrence so the diff reads as one consistent change.",
        "If an occurrence is a false positive or unsafe to change, leave it and say "
        "why in changes_summary.",
    ]
    description = (rule.get("description") or "").strip()
    if description:
        parts += ["", "## What the rule says", description[:3000]]
    profiles = dict.fromkeys(profile_for(o.issue["file_path"]) for o in primary)
    profiles.pop("", None)
    if profiles:
        parts += ["", "## The projects involved", *profiles]
    parts += ["", f"## Occurrences ({len(primary)})"]
    for occ in primary:
        parts += [
            "",
            f"### /{occ.issue['file_path']}:{occ.issue.get('line')}",
            f"Sonar: {occ.issue.get('message')}",
            "```",
            (occ.snippet or "(no snippet available - read the file)").rstrip(),
            "```",
        ]
    if notes:
        parts += ["", "## Reviewer notes - these override the above where they conflict", notes]
    parts += ["", "Read each file before editing it. Implement the fixes now."]
    return "\n".join(parts)


def _commit_if_changed(workspace: RepoWorkspace, message: str) -> tuple[str | None, str]:
    workspace.stage_all()
    diff = workspace.staged_diff()
    if not diff.strip():
        return None, ""
    return workspace.commit(message), diff


def run(batch_id: int) -> None:
    """Execute a queued batch. Safe to call from a background thread."""
    batch = store.get_batch(batch_id)
    if not batch or batch["status"] != "queued":
        return
    steps: list[dict[str, Any]] = []
    store.update_batch(batch_id, status="running", steps=steps)

    workspace = None
    base = branch = None
    try:
        selection = batch["selection"] or {}
        occurrences, workspace = classify(batch["project_key"])
        groups = {g.id: g for g in build_groups(occurrences)}
        mech_groups = [groups[g] for g in selection.get("mechanical", []) if g in groups]
        ai_groups = [groups[g] for g in selection.get("ai", []) if g in groups]

        base = workspace.current_branch()
        branch = workspace.start_branch(f"{branch_prefix()}/batch-{batch_id}")
        store.update_batch(batch_id, base_branch=base, branch=branch)

        if mech_groups:
            step = _step(steps, batch_id, kind="mechanical", title="Mechanical recipes", status="running")
            applied, failed = _apply_mechanical(workspace, mech_groups)
            rules = defaultdict(int)
            for item in applied:
                rules[item["recipe"]] += 1
            message = "\n".join(
                [
                    f"fix: apply {len(applied)} mechanical SonarQube fixes",
                    "",
                    "Deterministic recipes only - no AI. Each change was checked",
                    "against the exact code Sonar reported before it was made.",
                    "",
                    *[f"- {title} (x{n})" for title, n in rules.items()],
                ]
            )
            sha, _ = _commit_if_changed(workspace, message) if applied else (None, "")
            step.update(status="done" if sha else "no_change", commit=sha, applied=applied, failed=failed)
            _save(steps, batch_id)

        settings = get_settings()
        for group in ai_groups:
            step = _step(
                steps, batch_id, kind="ai", group=group.id, rule=group.rule,
                title=group.occurrences[0].issue.get("message"), status="running",
            )
            try:
                rule = sonar.get_rule(group.rule)
            except Exception:  # noqa: BLE001 - rule text is optional context
                rule = {"key": group.rule, "name": "", "description": ""}
            try:
                fix = engine.run_fix(
                    str(workspace.path),
                    _ai_task(group, rule, selection.get("notes", ""), settings.max_ai_occurrences),
                )
            except Exception as exc:  # noqa: BLE001 - one bad group must not sink the batch
                workspace.discard_changes()
                step.update(status="failed", error=f"AI session failed: {exc}")
                _save(steps, batch_id)
                continue
            primary = [o for o in group.occurrences if not o.duplicate_of]
            sha, _ = _commit_if_changed(
                workspace,
                f"fix({group.rule}): {primary[0].issue.get('message')}\n\n"
                f"AI fix, human-approved, {len(primary)} occurrence(s).\n\n"
                f"{fix.get('changes_summary', '')}".strip(),
            )
            step.update(
                status="done" if sha else "no_change",
                commit=sha,
                summary=fix.get("changes_summary"),
                testing=fix.get("testing_suggestions"),
                occurrences=[o.as_dict() for o in primary],
            )
            _save(steps, batch_id)

        diff = workspace.diff_between(base, branch)
        if not diff.strip():
            workspace.abandon(base, branch)
            store.update_batch(batch_id, status="no_changes", branch=None, steps=steps)
            return

        # Stop here - nothing builds or gets a PR until a human clicks
        # "Build" (see request_build). The workspace stays on `branch`;
        # that's what gets built.
        store.update_batch(batch_id, status="awaiting_build", diff=diff, steps=steps)
    except Exception as exc:  # noqa: BLE001 - record, never crash the worker
        if workspace and base:
            try:
                workspace.abandon(base, branch)
            except Exception:  # noqa: BLE001
                pass
        store.update_batch(
            batch_id, status="failed", steps=steps,
            error=f"{exc}\n\n{traceback.format_exc(limit=3)}",
        )


# --- build verification --------------------------------------------------------


def request_build(batch_id: int) -> dict[str, Any]:
    """Human clicked "Build": verify the batch's blast radius actually compiles.

    Never runs on its own right after the fixes are applied. On success,
    the PR title/description are generated and the batch is ready to
    publish; on failure the branch is discarded and the batch parks at
    'build_failed' for a human to retry (with feedback) or give up.
    """
    batch_row = store.get_batch(batch_id)
    if not batch_row:
        raise BatchError(f"Unknown batch: {batch_id}")
    if batch_row["status"] != "awaiting_build":
        raise BatchError(f"Batch {batch_id} is not waiting to be built.")

    project = store.get_project(batch_row["project_key"]) or {}
    workspace = RepoWorkspace(project["repo_path"])
    changed_files = workspace.diff_files_between(batch_row["base_branch"], batch_row["branch"])

    result = build.verify(str(workspace.path), changed_files)
    if not result["success"]:
        workspace.abandon(batch_row["base_branch"], batch_row["branch"])
        store.update_batch(
            batch_id, status="build_failed", branch=None,
            build_status=result["status"], build_output=result["output"],
        )
        return store.get_batch(batch_id) or {}

    title, description = pr_text(batch_row["project_key"], batch_row["steps"] or [])
    workspace.checkout(batch_row["base_branch"])  # leave the user's tree where it was
    store.update_batch(
        batch_id, status="ready", pr_title=title, pr_description=description,
        build_status=result["status"], build_output=result["output"],
    )
    return store.get_batch(batch_id) or {}


def retry_with_feedback(batch_id: int, feedback: str = "") -> int:
    """A build failed; start a fresh batch with the same selection, feeding
    the AI groups the build error and the reviewer's feedback as reviewer
    notes. Returns the new batch id - run it the same way as a fresh one."""
    old = store.get_batch(batch_id)
    if not old:
        raise BatchError(f"Unknown batch: {batch_id}")
    if old["status"] != "build_failed":
        raise BatchError(f"Batch {batch_id} has no failed build to retry.")

    selection = dict(old["selection"] or {})
    notes_parts = [(selection.get("notes") or "").strip()]
    if old.get("build_output"):
        notes_parts.append(
            "## The previous attempt did not build - fix this too\n```\n"
            + old["build_output"][:3000] + "\n```"
        )
    if feedback.strip():
        notes_parts.append(
            "## Reviewer notes on the build failure - address these\n" + feedback.strip()
        )
    selection["notes"] = "\n\n".join(p for p in notes_parts if p)

    new_id = store.create_batch(old["project_key"], selection)
    store.update_batch(new_id, retry_of=batch_id)
    return new_id


def abandon(batch_id: int) -> dict[str, Any]:
    """A build failed and the reviewer is done retrying - mark it terminal."""
    batch_row = store.get_batch(batch_id)
    if not batch_row:
        raise BatchError(f"Unknown batch: {batch_id}")
    if batch_row["status"] != "build_failed":
        raise BatchError(f"Batch {batch_id} has no failed build to abandon.")
    store.update_batch(batch_id, status="failed")
    return store.get_batch(batch_id) or {}


# --- pull request ------------------------------------------------------------


def pr_text(project_key: str, steps: list[dict[str, Any]]) -> tuple[str, str]:
    """Title and description built from the batch results - no LLM."""
    project = store.get_project(project_key) or {}
    mech = next((s for s in steps if s["kind"] == "mechanical"), None)
    ai_steps = [s for s in steps if s["kind"] == "ai" and s.get("commit")]
    n_mech = len(mech.get("applied", [])) if mech and mech.get("commit") else 0
    n_ai = sum(len(s.get("occurrences", [])) for s in ai_steps)

    title = f"SonarFix: resolve {n_mech + n_ai} SonarQube issues"
    lines = [
        "## Summary",
        f"Resolves **{n_mech + n_ai}** SonarQube issues in **{project.get('name') or project_key}**: "
        f"{n_mech} with deterministic recipes (no AI) and {n_ai} with AI in "
        f"{len(ai_steps)} human-approved group(s).",
    ]
    if n_mech:
        lines += ["", "## Mechanical fixes (no AI)", "", "| File | Line | Fix |", "|---|---|---|"]
        lines += [f"| `{a['file']}` | {a['line']} | {a['recipe']} |" for a in mech["applied"]]
    if ai_steps:
        lines += ["", "## AI fixes (human-approved)"]
        for s in ai_steps:
            lines += ["", f"### `{s['rule']}` - {s.get('title')}", "", (s.get("summary") or "").strip()]
            lines += [f"- `{o['file']}:{o['line']}`" for o in s.get("occurrences", [])]
    skipped = [f for s in steps for f in s.get("failed", [])] + [
        {"file": s["rule"], "line": "-", "reason": s.get("error") or s.get("summary") or "no change"}
        for s in steps
        if s["kind"] == "ai" and not s.get("commit")
    ]
    if skipped:
        lines += ["", "## Not changed", ""]
        lines += [f"- `{f['file']}:{f['line']}` - {f['reason']}" for f in skipped]
    lines += [
        "",
        "## Reviewer checklist",
        "- [ ] Build passes",
        "- [ ] Tests pass",
        "- [ ] AI-authored changes reviewed line by line",
        "",
        "_Generated by SonarFix._",
    ]
    return title, "\n".join(lines)


def create_pull_request(batch_id: int) -> dict[str, Any]:
    batch = store.get_batch(batch_id)
    if not batch:
        raise BatchError(f"Unknown batch: {batch_id}")
    if batch["status"] == "pr_open":
        return batch
    if batch["status"] != "ready":
        raise BatchError(f"Batch is {batch['status']}; only a ready batch can be published.")

    project = store.get_project(batch["project_key"]) or {}
    repo_path = project.get("repo_path")
    readiness = scm.status(repo_path)
    if not readiness["ready"]:
        raise BatchError(str(readiness["reason"]))

    scm.push(repo_path, batch["branch"])
    url = scm.open_pull_request(
        repo_path, batch["branch"], batch["base_branch"], batch["pr_title"], batch["pr_description"]
    )
    store.update_batch(batch_id, status="pr_open", pr_url=url)
    return store.get_batch(batch_id)


# --- single issue ------------------------------------------------------------


def fix_single(issue_id: str, notes: str = "") -> dict[str, Any]:
    """Fix exactly one issue on its own branch, and report what happened.

    Used by the one-issue-at-a-time agent. A mechanical issue costs nothing;
    anything else gets one AI session scoped to that single occurrence.
    """
    issue = store.get_issue(issue_id)
    if not issue:
        raise BatchError(f"Unknown issue: {issue_id}")
    project_key = issue["project_key"]

    occurrences, workspace = classify(project_key)
    target = next((o for o in occurrences if o.issue["id"] == issue_id), None)
    if target is None:
        raise BatchError(f"{issue_id} is not in the current plan for {project_key}.")
    if target.bucket == "skip":
        raise BatchError(f"{issue_id} is in skipped code: {target.skip_reason}")
    if workspace.is_dirty():
        raise BatchError(
            f"{workspace.path} has uncommitted changes; commit or stash them first."
        )

    batch_id = store.create_batch(
        project_key, {"mechanical": [], "ai": [], "notes": notes, "singleIssue": issue_id}
    )
    base = workspace.current_branch()
    branch = workspace.start_branch(f"{branch_prefix()}/issue-{issue_id[:12].lower()}")
    store.update_batch(batch_id, status="running", base_branch=base, branch=branch)

    steps: list[dict[str, Any]] = []
    try:
        if target.bucket == "mechanical":
            group = Group("mechanical", target.issue.get("rule") or "unknown", [target])
            applied, failed = _apply_mechanical(workspace, [group])
            if not applied:
                reason = failed[0]["reason"] if failed else "the recipe did not apply"
                raise BatchError(f"Mechanical fix did not apply: {reason}")
            sha, _ = _commit_if_changed(
                workspace,
                f"fix({target.issue['rule']}): {target.recipe.title}\n\n"
                f"{target.issue.get('message')}\n"
                f"{target.issue['file_path']}:{target.issue.get('line')}\n\n"
                "Deterministic recipe - no AI was used.",
            )
            steps.append(
                {
                    "kind": "mechanical",
                    "title": target.recipe.title,
                    "status": "done" if sha else "no_change",
                    "commit": sha,
                    "applied": applied,
                    "failed": failed,
                }
            )
        else:
            group = Group("ai", target.issue.get("rule") or "unknown", [target])
            try:
                rule = sonar.get_rule(group.rule)
            except Exception:  # noqa: BLE001 - rule text is optional context
                rule = {"key": group.rule, "name": "", "description": ""}
            fix = engine.run_fix(str(workspace.path), _ai_task(group, rule, notes, 1))
            sha, _ = _commit_if_changed(
                workspace,
                f"fix({group.rule}): {target.issue.get('message')}\n\n"
                f"{target.issue['file_path']}:{target.issue.get('line')}\n\n"
                f"{fix.get('changes_summary', '')}".strip(),
            )
            steps.append(
                {
                    "kind": "ai",
                    "rule": group.rule,
                    "title": target.issue.get("message"),
                    "status": "done" if sha else "no_change",
                    "commit": sha,
                    "summary": fix.get("changes_summary"),
                    "testing": fix.get("testing_suggestions"),
                    "occurrences": [target.as_dict()],
                }
            )

        diff = workspace.diff_between(base, branch)
        if not diff.strip():
            workspace.abandon(base, branch)
            store.update_batch(batch_id, status="no_changes", branch=None, steps=steps)
            return {**(store.get_batch(batch_id) or {}), "issueId": issue_id}

        title, description = pr_text(project_key, steps)
        workspace.checkout(base)
        store.update_batch(
            batch_id,
            status="ready",
            diff=diff,
            pr_title=title,
            pr_description=description,
            steps=steps,
        )
    except Exception as exc:  # noqa: BLE001 - always leave the tree as we found it
        try:
            workspace.abandon(base, branch)
        except Exception:  # noqa: BLE001
            pass
        store.update_batch(batch_id, status="failed", steps=steps, error=str(exc))
        raise

    return {**(store.get_batch(batch_id) or {}), "issueId": issue_id}
