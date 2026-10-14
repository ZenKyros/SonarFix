"""Self-contained HTML report: project, issues, and their remediation plans."""

from __future__ import annotations

import html
from datetime import datetime, timezone
from typing import Any

from . import graph as workflow
from . import planner, store

SEVERITY_ORDER = ["BLOCKER", "CRITICAL", "MAJOR", "MINOR", "INFO"]
SEVERITY_COLOR = {
    "BLOCKER": "#8b0000",
    "CRITICAL": "#d64545",
    "MAJOR": "#e08e2b",
    "MINOR": "#3b82c4",
    "INFO": "#8a8f98",
}
STATUS_LABEL = {
    "pending": "Awaiting review",
    "approved": "Approved & fixed",
    "rejected": "Rejected",
}
CHECK_COLOR = {"passed": "#0a6b34", "failed": "#a3231f", "skipped": "#91600a"}
CHECK_BG = {"passed": "#d1f4de", "failed": "#fbdada", "skipped": "#fde9c8"}
CHECK_ICON = {"passed": "✓", "failed": "✕", "skipped": "⚠"}


def _esc(value: Any) -> str:
    return html.escape(str(value)) if value is not None else ""


def _check_chip(status: str | None, label: str) -> str:
    if not status:
        return ""
    bg = CHECK_BG.get(status, "#e2e4e8")
    color = CHECK_COLOR.get(status, "#555")
    icon = CHECK_ICON.get(status, "•")
    return (
        f'<span class="check-chip" style="background:{bg};color:{color}">'
        f"{icon} {_esc(label)}: {_esc(status)}</span>"
    )


def _plan_for(issue_id: str) -> dict[str, Any] | None:
    plan = store.latest_plan(issue_id)
    if not plan:
        return None
    payload = dict(plan)
    payload["remediation_plan"] = workflow.plan_steps(plan.get("plan_json"))
    return payload


def _run_for(plan_id: int | None) -> dict[str, Any] | None:
    if not plan_id:
        return None
    run = store.latest_run(int(plan_id))
    return dict(run) if run else None


def _issue_card(issue: dict[str, Any], resolved_by: dict[str, int]) -> str:
    severity = issue.get("severity") or "INFO"
    color = SEVERITY_COLOR.get(severity, "#8a8f98")
    line = f":{issue['line']}" if issue.get("line") else ""
    plan = _plan_for(issue["id"])
    run = _run_for(plan.get("id")) if plan else None
    batch_id = resolved_by.get(issue["id"])

    body = f"""
    <div class="issue" id="issue-{_esc(issue['id'])}">
      <div class="issue-head">
        <span class="badge" style="background:{color}">{_esc(severity)}</span>
        <span class="rule">{_esc(issue.get('rule'))}</span>
        <span class="loc">{_esc(issue.get('file_path'))}{line}</span>
      </div>
      <p class="message">{_esc(issue.get('message'))}</p>
    """

    if plan:
        confidence = plan.get("confidence")
        conf_pct = f"{float(confidence):.0%}" if confidence is not None else "—"
        status = plan.get("status") or "pending"
        steps = plan.get("remediation_plan") or []
        steps_html = "".join(f"<li>{_esc(step)}</li>" for step in steps)

        body += f"""
      <div class="plan">
        <div class="plan-head">
          <strong>AI analysis</strong>
          <span class="status status-{_esc(status)}">{_esc(STATUS_LABEL.get(status, status))}</span>
          <span class="confidence">Confidence {_esc(conf_pct)}</span>
        </div>
        <dl>
          <dt>What this means</dt><dd>{_esc(plan.get('explanation') or '—')}</dd>
          <dt>Root cause</dt><dd>{_esc(plan.get('root_cause') or '—')}</dd>
          <dt>Impact</dt><dd>{_esc(plan.get('impact') or '—')}</dd>
        </dl>
        {"<div class='steps'><strong>Remediation plan</strong><ol>" + steps_html + "</ol></div>" if steps else ""}
        {f"<dl><dt>How to verify</dt><dd>{_esc(plan.get('testing_notes'))}</dd></dl>" if plan.get('testing_notes') else ""}
      </div>
      """

        if run and run.get("status") == "applied":
            diff = run.get("diff") or ""
            commit_msg = run.get("commit_message") or ""
            checks = (
                _check_chip(run.get("build_status"), "Build")
                + _check_chip(run.get("test_status"), "Test")
            )
            artifacts = run.get("artifacts") or []
            artifacts_html = "".join(
                f"<li><code>{_esc(a.get('file'))}</code> "
                f"<span class='muted'>({_esc(a.get('kind'))})</span>"
                + (
                    "<ul class='artifact-files'>"
                    + "".join(f"<li><code>{_esc(f)}</code></li>" for f in a.get("files") or [])
                    + "</ul>"
                    if a.get("files")
                    else ""
                )
                + "</li>"
                for a in artifacts
            )
            build_log_html = (
                f"<div class='terminal-label'>$ build log</div><pre class='diff'>{_esc(run.get('build_output'))}</pre>"
                if run.get("build_output")
                else ""
            )
            test_log_html = (
                f"<div class='terminal-label'>$ test log</div><pre class='diff'>{_esc(run.get('test_output'))}</pre>"
                if run.get("test_output")
                else ""
            )
            pr_url = run.get("pr_url")
            pr_html = (
                f'<span class="status status-approved">✓ PR created: '
                f'<a href="{_esc(pr_url)}" target="_blank" rel="noreferrer">{_esc(pr_url)}</a></span>'
                if pr_url
                else f'<span class="status status-pending">branch {_esc(run.get("branch"))} - no PR yet</span>'
            )
            body += f"""
      <div class="fix">
        <div class="plan-head"><strong>Fix applied</strong>
          {pr_html}
          {checks}
        </div>
        {f"<dl><dt>Commit message</dt><dd><pre>{_esc(commit_msg)}</pre></dd></dl>" if commit_msg else ""}
        {f"<div class='artifacts'><strong>Affected build artifacts</strong><ul>{artifacts_html}</ul></div>" if artifacts else ""}
        {build_log_html}
        {test_log_html}
        {"<div class='diff-label'>Before &rarr; after (unified diff)</div>" if diff else ""}
        {f"<pre class='diff'>{_esc(diff)}</pre>" if diff else ""}
      </div>
      """
    elif batch_id is not None:
        body += (
            f'<p class="status status-approved">✓ Fixed by batch #{batch_id} '
            f'— see "Resolved fixes" below for the full report.</p>'
        )
    else:
        body += '<p class="no-plan">Not analyzed yet.</p>'

    body += "</div>"
    return body


def _batch_card(batch: dict[str, Any]) -> str:
    """A resolved batch (mechanical/AI fixes applied together, build+test
    verified) in the same level of detail as the single-issue cards above -
    confidence, build/test logs, artifacts, before/after diff."""
    confidence = planner.confidence_for(batch)
    conf_color = {"high": "#0a6b34", "medium": "#91600a", "low": "#a3231f"}.get(confidence["level"], "#555")

    checks = _check_chip(batch.get("build_status"), "Build") + _check_chip(batch.get("test_status"), "Test")

    artifacts = batch.get("artifacts") or []
    artifacts_html = "".join(
        f"<li><code>{_esc(a.get('file'))}</code> <span class='muted'>({_esc(a.get('kind'))})</span>"
        + (
            "<ul class='artifact-files'>"
            + "".join(f"<li><code>{_esc(f)}</code></li>" for f in a.get("files") or [])
            + "</ul>"
            if a.get("files")
            else ""
        )
        + "</li>"
        for a in artifacts
    )

    steps_html = ""
    for step in batch.get("steps") or []:
        title = "Mechanical recipes" if step.get("kind") == "mechanical" else step.get("title", "")
        occs = step.get("applied") if step.get("kind") == "mechanical" else step.get("occurrences")
        occ_html = "".join(
            f"<li><code>{_esc(o.get('file'))}:{_esc(o.get('line'))}</code> {_esc(o.get('message'))}</li>"
            for o in occs or []
        )
        steps_html += f"""
        <div class="plan">
          <strong>{_esc(title)}</strong>
          {f"<p class='why'>{_esc(step.get('summary'))}</p>" if step.get('summary') else ""}
          {f"<dl><dt>How to verify</dt><dd>{_esc(step.get('testing'))}</dd></dl>" if step.get('testing') else ""}
          <ul>{occ_html}</ul>
        </div>
        """

    pr_url = batch.get("pr_url")
    pr_html = (
        f'<span class="status status-approved">✓ PR created: '
        f'<a href="{_esc(pr_url)}" target="_blank" rel="noreferrer">{_esc(pr_url)}</a></span>'
        if pr_url
        else f'<span class="status status-pending">branch {_esc(batch.get("branch"))} - no PR yet</span>'
    )

    build_log_html = (
        f"<div class='terminal-label'>$ build log</div><pre class='diff'>{_esc(batch.get('build_output'))}</pre>"
        if batch.get("build_output")
        else ""
    )
    test_log_html = (
        f"<div class='terminal-label'>$ test log</div><pre class='diff'>{_esc(batch.get('test_output'))}</pre>"
        if batch.get("test_output")
        else ""
    )

    return f"""
    <div class="issue" id="batch-{_esc(batch['id'])}">
      <div class="issue-head">
        <span class="badge" style="background:{conf_color}">{_esc(confidence['level'].upper())} CONFIDENCE</span>
        <span class="rule">{_esc(batch.get('pr_title') or f"Batch #{batch['id']}")}</span>
        <span class="loc">{_esc(batch.get('branch'))}</span>
      </div>
      <p class="message">{_esc(confidence['reason'])}</p>
      <div class="fix">
        <div class="plan-head">{pr_html}{checks}</div>
        {steps_html}
        {f"<div class='artifacts'><strong>Affected build artifacts</strong><ul>{artifacts_html}</ul></div>" if artifacts else ""}
        {build_log_html}
        {test_log_html}
        {"<div class='diff-label'>Before &rarr; after (unified diff)</div>" if batch.get('diff') else ""}
        {f"<pre class='diff'>{_esc(batch.get('diff'))}</pre>" if batch.get('diff') else ""}
      </div>
    </div>
    """


_PAGE_CSS = """
  :root { color-scheme: light; }
  * { box-sizing: border-box; }
  body {
    font-family: -apple-system, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
    background: #f5f6f8; color: #1c2024; margin: 0; padding: 40px 24px;
    line-height: 1.5;
  }
  .wrap { max-width: 900px; margin: 0 auto; }
  h1 { font-size: 1.6rem; margin: 0 0 4px; }
  .meta { color: #666; font-size: 0.9rem; margin-bottom: 20px; }
  .summary {
    display: flex; gap: 10px; flex-wrap: wrap; align-items: center;
    background: #fff; border: 1px solid #e2e4e8; border-radius: 10px;
    padding: 16px 20px; margin-bottom: 28px;
  }
  .summary .totals { font-weight: 600; margin-right: 8px; }
  .chip {
    color: #fff; font-size: 0.78rem; font-weight: 600; padding: 3px 10px;
    border-radius: 999px; letter-spacing: 0.02em;
  }
  .issue {
    background: #fff; border: 1px solid #e2e4e8; border-radius: 10px;
    padding: 18px 20px; margin-bottom: 16px;
  }
  .issue-head { display: flex; align-items: center; gap: 10px; margin-bottom: 8px; flex-wrap: wrap; }
  .badge { color: #fff; font-size: 0.72rem; font-weight: 700; padding: 3px 9px; border-radius: 6px; }
  .rule { font-family: ui-monospace, SFMono-Regular, Consolas, monospace; font-size: 0.82rem; color: #555; }
  .loc { font-family: ui-monospace, SFMono-Regular, Consolas, monospace; font-size: 0.8rem; color: #999; margin-left: auto; }
  .message { font-size: 1rem; margin: 6px 0 14px; }
  .no-plan { color: #999; font-style: italic; font-size: 0.9rem; }
  .plan, .fix { background: #fafbfc; border: 1px solid #eceef1; border-radius: 8px; padding: 14px 16px; margin-top: 10px; }
  .plan-head { display: flex; align-items: center; gap: 10px; margin-bottom: 10px; flex-wrap: wrap; }
  .status { font-size: 0.75rem; padding: 2px 9px; border-radius: 999px; background: #e2e4e8; }
  .status-approved { background: #d1f4de; color: #0a6b34; }
  .status-rejected { background: #fbdada; color: #a3231f; }
  .status-pending { background: #fde9c8; color: #91600a; }
  .confidence { font-size: 0.8rem; color: #666; margin-left: auto; }
  dl { margin: 0 0 10px; }
  dt { font-size: 0.75rem; text-transform: uppercase; letter-spacing: 0.04em; color: #888; margin-top: 8px; }
  dd { margin: 2px 0 0; font-size: 0.92rem; }
  .steps ol { margin: 6px 0 0; padding-left: 20px; }
  .steps li { margin-bottom: 4px; font-size: 0.92rem; }
  pre { background: #1c2024; color: #e2e4e8; padding: 12px 14px; border-radius: 6px;
         overflow-x: auto; font-size: 0.8rem; white-space: pre-wrap; word-break: break-word; }
  pre.diff { font-family: ui-monospace, SFMono-Regular, Consolas, monospace; }
  .check-chip { font-size: 0.75rem; font-weight: 600; padding: 2px 9px; border-radius: 999px; }
  .diff-label { font-size: 0.75rem; text-transform: uppercase; letter-spacing: 0.04em; color: #888; margin: 10px 0 4px; }
  .artifacts { margin-top: 10px; }
  .artifacts ul { margin: 6px 0 0; padding-left: 18px; }
  .artifacts li { font-size: 0.88rem; margin-bottom: 2px; }
  .artifact-files { padding-left: 16px; margin: 2px 0 4px; }
  .artifact-files li { font-size: 0.8rem; color: #888; }
  .muted { color: #888; }
  .terminal-label { font-size: 0.75rem; text-transform: uppercase; letter-spacing: 0.04em; color: #888; margin: 10px 0 4px; }
  .section-head { font-size: 1.15rem; margin: 32px 0 4px; }
"""


def _page_shell(title: str, body: str) -> str:
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>{_esc(title)}</title>
<style>{_PAGE_CSS}</style>
</head>
<body>
<div class="wrap">
{body}
</div>
</body>
</html>"""


def generate_batch_report(batch_id: int) -> str:
    """A single batch's full detail report, downloadable straight from its
    own PR/batch page - the same card shown in the project report's
    "Resolved fixes" section and the Plan page's "PR Created" tab, standalone."""
    batch = store.get_batch(batch_id)
    if not batch:
        raise ValueError(f"Unknown batch: {batch_id}")
    project = store.get_project(batch["project_key"]) or {
        "key": batch["project_key"], "name": batch["project_key"]
    }
    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    title = batch.get("pr_title") or f"Batch #{batch_id}"

    body = f"""
  <h1>{_esc(title)}</h1>
  <div class="meta">
    {_esc(project['name'])} ({_esc(project['key'])}) &middot; branch {_esc(batch.get('branch'))}
    &middot; Generated {_esc(generated_at)}
  </div>
  {_batch_card(batch)}
  """
    return _page_shell(f"SonarFix — {title}", body)


def generate_html_report(project_key: str) -> str:
    """A single, dependency-free HTML file: project name, every issue, and its
    remediation plan/fix status where one exists - whether that fix came
    from the single-issue AI flow (fix_plans/fix_runs) or a batch run
    (mechanical recipes and/or AI, see `store.resolved_batches`)."""
    project = store.get_project(project_key) or {"key": project_key, "name": project_key}
    issues = store.list_issues(project_key)
    resolved_by = store.resolved_issue_ids(project_key)
    batches = store.resolved_batches(project_key)
    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    by_severity: dict[str, int] = {}
    analyzed = 0
    fixed = 0
    for issue in issues:
        by_severity[issue.get("severity") or "INFO"] = (
            by_severity.get(issue.get("severity") or "INFO", 0) + 1
        )
        if issue["id"] in resolved_by:
            fixed += 1
            continue
        plan = _plan_for(issue["id"])
        if plan:
            analyzed += 1
            run = _run_for(plan.get("id"))
            if run and run.get("status") == "applied":
                fixed += 1

    summary_chips = "".join(
        f'<span class="chip" style="background:{SEVERITY_COLOR.get(s, "#8a8f98")}">'
        f"{s}: {by_severity.get(s, 0)}</span>"
        for s in SEVERITY_ORDER
        if by_severity.get(s)
    )

    issue_cards = "".join(_issue_card(issue, resolved_by) for issue in issues) or (
        '<p class="no-plan">No open issues.</p>'
    )

    batch_section = ""
    if batches:
        batch_cards = "".join(_batch_card(b) for b in batches)
        batch_section = f"""
  <h2 class="section-head">Resolved fixes ({len(batches)} batch{"" if len(batches) == 1 else "es"})</h2>
  <p class="meta">Fixes applied together as a batch (mechanical recipes and/or AI), with their build/test
    verification and pull request status - independent of the per-issue cards above.</p>
  {batch_cards}
  """

    body = f"""
  <h1>{_esc(project['name'])}</h1>
  <div class="meta">Project key: {_esc(project['key'])} · Generated {_esc(generated_at)}</div>
  <div class="summary">
    <span class="totals">{len(issues)} issues</span>
    {summary_chips}
    <span style="margin-left:auto;color:#666;font-size:0.85rem">
      {analyzed} analyzed · {fixed} fixed
    </span>
  </div>
  {issue_cards}
  {batch_section}
  """
    return _page_shell(f"SonarFix report — {project['name']}", body)
