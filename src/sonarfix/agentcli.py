"""JSON bridge between the PowerShell agents and the tested remediation core.

PowerShell owns the .NET work (discovery, MSBuild, Sonar scanning). Everything
that decides *what to change* lives here, because it is already covered by
tests: deterministic recipes, issue grouping, and the Bitbucket client.

Every command prints one JSON object on stdout and exits 0, or prints
{"ok": false, "error": ...} and exits 1. Nothing else is written to stdout,
so the caller can parse it directly.

    python -m sonarfix.agentcli import-issues --project KEY --repo PATH --issues issues.json
    python -m sonarfix.agentcli plan          --project KEY
    python -m sonarfix.agentcli apply         --project KEY --selection sel.json
    python -m sonarfix.agentcli pull-request  --batch 3
    python -m sonarfix.agentcli scm-status    --project KEY
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .core import batch, planner, scm, store


def _emit(payload: dict[str, Any]) -> int:
    json.dump(payload, sys.stdout, indent=2, default=str)
    sys.stdout.write("\n")
    return 0


def _load(path: str) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def cmd_import_issues(args: argparse.Namespace) -> int:
    """Register the project, attach the clone, and load a Sonar issue dump."""
    store.init_db()
    store.upsert_projects([{"key": args.project, "name": args.name or args.project}])
    store.set_repo_path(args.project, str(Path(args.repo).resolve()))

    payload = _load(args.issues)
    # Accept either the raw SonarQube response or a bare list of issues.
    issues = payload.get("issues", payload) if isinstance(payload, dict) else payload
    open_issues = [i for i in issues if i.get("status") not in {"RESOLVED", "CLOSED"}]

    count = store.replace_issues(args.project, open_issues)
    store.mark_synced(args.project)
    return _emit(
        {
            "ok": True,
            "project": args.project,
            "repoPath": store.get_project(args.project)["repo_path"],
            "received": len(issues),
            "imported": count,
            "skippedResolved": len(issues) - len(open_issues),
        }
    )


def cmd_plan(args: argparse.Namespace) -> int:
    """Group issues and route each to a recipe, to AI, or to skip. No LLM calls."""
    plan = planner.build_plan(args.project)
    summary = plan["summary"]
    plan["ok"] = True
    plan["costModel"] = {
        "mechanicalIssues": summary["mechanical"],
        "mechanicalTokens": 0,
        "aiGroups": summary["ai_groups"],
        "aiIssues": summary["ai"],
        # One AI session per group, not per issue: that ratio is the saving.
        "aiSessionsIfApproved": summary["ai_groups"],
        "issuesPerSession": round(summary["ai"] / summary["ai_groups"], 1)
        if summary["ai_groups"]
        else 0,
    }
    return _emit(plan)


def cmd_apply(args: argparse.Namespace) -> int:
    """Run the approved groups: recipes first, then any approved AI groups."""
    selection = _load(args.selection)
    ai_groups = selection.get("ai", [])
    batch_id = batch.create(
        args.project,
        selection.get("mechanical", []),
        ai_groups,
        # AI never runs unless the selection says a human approved it.
        approve_ai=bool(selection.get("approveAi")) and bool(ai_groups),
        notes=selection.get("notes", ""),
    )
    batch.run(batch_id)
    result = store.get_batch(batch_id) or {}
    steps = result.get("steps") or []
    applied = sum(len(s.get("applied", [])) for s in steps if s["kind"] == "mechanical")
    ai_fixed = sum(
        len(s.get("occurrences", []))
        for s in steps
        if s["kind"] == "ai" and s.get("commit")
    )
    return _emit(
        {
            "ok": result.get("status") in {"ready", "no_changes"},
            "batchId": batch_id,
            "status": result.get("status"),
            "branch": result.get("branch"),
            "baseBranch": result.get("base_branch"),
            "error": result.get("error"),
            "mechanicalFixes": applied,
            "aiFixes": ai_fixed,
            "aiSessions": len([s for s in steps if s["kind"] == "ai"]),
            "prTitle": result.get("pr_title"),
            "steps": steps,
        }
    )


def cmd_fix_one(args: argparse.Namespace) -> int:
    """Fix exactly one issue on its own branch."""
    result = batch.fix_single(args.issue, notes=args.notes or "")
    steps = result.get("steps") or []
    step = steps[0] if steps else {}
    return _emit(
        {
            "ok": result.get("status") in {"ready", "no_changes"},
            "batchId": result.get("id"),
            "issueId": args.issue,
            "status": result.get("status"),
            "branch": result.get("branch"),
            "baseBranch": result.get("base_branch"),
            "kind": step.get("kind"),
            "usedAi": step.get("kind") == "ai",
            "commit": step.get("commit"),
            "summary": step.get("summary") or step.get("title"),
            "testing": step.get("testing"),
            "prTitle": result.get("pr_title"),
            "diff": result.get("diff"),
            "error": result.get("error"),
        }
    )


def cmd_pull_request(args: argparse.Namespace) -> int:
    result = batch.create_pull_request(args.batch)
    return _emit(
        {
            "ok": result["status"] == "pr_open",
            "batchId": result["id"],
            "status": result["status"],
            "branch": result["branch"],
            "prUrl": result["pr_url"],
        }
    )


def cmd_scm_status(args: argparse.Namespace) -> int:
    project = store.get_project(args.project) or {}
    status = scm.status(project.get("repo_path"))
    return _emit({"ok": True, **status})


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="sonarfix.agentcli", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("import-issues")
    p.add_argument("--project", required=True)
    p.add_argument("--repo", required=True)
    p.add_argument("--issues", required=True)
    p.add_argument("--name")
    p.set_defaults(func=cmd_import_issues)

    p = sub.add_parser("plan")
    p.add_argument("--project", required=True)
    p.set_defaults(func=cmd_plan)

    p = sub.add_parser("apply")
    p.add_argument("--project", required=True)
    p.add_argument("--selection", required=True)
    p.set_defaults(func=cmd_apply)

    p = sub.add_parser("fix-one")
    p.add_argument("--issue", required=True)
    p.add_argument("--notes")
    p.set_defaults(func=cmd_fix_one)

    p = sub.add_parser("pull-request")
    p.add_argument("--batch", type=int, required=True)
    p.set_defaults(func=cmd_pull_request)

    p = sub.add_parser("scm-status")
    p.add_argument("--project", required=True)
    p.set_defaults(func=cmd_scm_status)

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except Exception as exc:  # noqa: BLE001 - the caller parses this, never a traceback
        json.dump({"ok": False, "error": str(exc)}, sys.stdout, indent=2)
        sys.stdout.write("\n")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
