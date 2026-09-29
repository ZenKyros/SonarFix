"""Prepare the DomainUpdateService demo from a real SonarQube scan.

Takes the findings already on the local SonarQube server and the source they
were reported against, and leaves them in a state the UI can work through:
read an issue, see why it fails, apply the fix, download the report.

Two things it arranges that a scan does not:

  * A git repository. Fixes are committed on a branch, so the source has to be
    under version control; the scanned tree is a downloaded copy with no .git.
  * Issues in the local store, keyed to that repository, so the file each
    finding names actually resolves.

Build output is left behind: obj/, bin/ and .sonarqube hold compiler artifacts
and vendored copies, and a fix must never land in them.

    python scripts/demo_setup.py
    python scripts/demo_setup.py --prewarm     # analyse ahead of a live demo
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

PROJECT_KEY = "InfoImage_CK"
PROJECT_NAME = "InfoImage - DomainUpdateService"

DEFAULT_SOURCE = (
    r"C:\Users\YadavRos\Downloads"
    r"\10.0-InfoImage11.10Support-CK@5d21743554e\DomainUpdateService"
)

# Compiler output and scanner scratch. Never source, never fixable.
EXCLUDE_DIRS = {"obj", "bin", ".sonarqube", ".vs", "packages", "TestResults"}


def run_git(repo: Path, *args: str) -> str:
    out = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)
    if out.returncode != 0:
        raise SystemExit(f"git {' '.join(args)} failed:\n{out.stderr}")
    return out.stdout


def copy_source(source: Path, dest: Path) -> int:
    if dest.exists():
        shutil.rmtree(dest, ignore_errors=True)
    dest.mkdir(parents=True)

    copied = 0
    for src in source.rglob("*"):
        if not src.is_file():
            continue
        rel = src.relative_to(source)
        if any(part in EXCLUDE_DIRS for part in rel.parts):
            continue
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, target)
        copied += 1
    return copied


def fetch_issues(project_key: str) -> list[dict]:
    """Every open finding SonarQube holds for this project."""
    from sonarfix.core import sonar

    return sonar.list_issues(project_key)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", default=DEFAULT_SOURCE)
    parser.add_argument("--dest", default=str(ROOT / "demo" / "DomainUpdateService"))
    parser.add_argument("--project", default=PROJECT_KEY)
    parser.add_argument(
        "--prewarm",
        action="store_true",
        help="analyse the top issues now. Analysis is the slowest step in the "
        "flow at minutes per issue, so running it beforehand leaves the root "
        "cause already on screen when the demo starts.",
    )
    parser.add_argument("--prewarm-count", type=int, default=3)
    parser.add_argument(
        "--remote",
        default="https://ustr-bitbucket-1.na.uis.unisys.com/scm/INF/10.0.git",
        help="origin for the demo clone; nothing is pushed unless you ask",
    )
    args = parser.parse_args()

    source = Path(args.source)
    repo = Path(args.dest)
    if not source.is_dir():
        raise SystemExit(f"Scanned source not found: {source}")

    # --- the source, under version control ----------------------------------
    copied = copy_source(source, repo)
    print(f"[OK]   copied {copied} file(s) from the scanned tree")

    run_git(repo, "init", "-q", "-b", "main")
    run_git(repo, "config", "user.email", "sonarfix@unisys.com")
    run_git(repo, "config", "user.name", "SonarFix")
    # Legacy sources are CRLF on disk. Keep the bytes as they are, so the
    # diffs shown are real diffs and not a line-ending rewrite.
    run_git(repo, "config", "core.autocrlf", "false")
    (repo / ".gitignore").write_text(
        "\n".join(sorted(EXCLUDE_DIRS)) + "\n", encoding="utf-8"
    )
    run_git(repo, "add", "-A")
    run_git(repo, "commit", "-q", "-m", "DomainUpdateService as scanned")
    run_git(repo, "remote", "add", "origin", args.remote)
    print("[OK]   git repository ready on 'main'")

    # --- the findings -------------------------------------------------------
    os.environ.setdefault("SONARFIX_DB", str(ROOT / "data" / "sonarfix.db"))
    from sonarfix.core import store

    store.init_db()
    store.upsert_projects([{"key": args.project, "name": PROJECT_NAME}])
    store.set_repo_path(args.project, str(repo.resolve()))

    issues = fetch_issues(args.project)
    if not issues:
        raise SystemExit(
            f"SonarQube returned no open issues for {args.project!r}. "
            "Check SONAR_URL and SONAR_TOKEN in .env."
        )
    count = store.replace_issues(args.project, issues)
    store.mark_synced(args.project)
    print(f"[OK]   imported {count} issue(s) from SonarQube")

    # Confirm the paths line up: an issue whose file does not resolve cannot
    # be fixed, and that is worth knowing now rather than mid-demo.
    missing = [
        i for i in store.list_issues(args.project) if not (repo / i["file_path"]).is_file()
    ]
    if missing:
        print(f"[WARN] {len(missing)} issue(s) name a file that is not in the repo")
        for i in missing[:3]:
            print(f"         {i['file_path']}")
    else:
        print("[OK]   every issue resolves to a file in the repo")

    # --- what the planner will do with them ---------------------------------
    from sonarfix.core import planner

    plan = planner.build_plan(args.project)
    s = plan["summary"]
    print()
    print(f"  {s['total']} issue(s) in {s['groups']} pattern(s)")
    print(f"    {s['mechanical']:>4}  deterministic recipe   (no model call)")
    print(f"    {s['ai']:>4}  need analysis          ({s['ai_groups']} group(s))")
    print(f"    {s['skip']:>4}  skipped")

    mech = [g for g in plan["groups"] if g["bucket"] == "mechanical"]
    if mech:
        print()
        print("  Fixable without a model call:")
        for g in mech:
            print(f"    {g['unique']:>4}  {g['rule']:<24} {g['recipe']['title']}")

    if args.prewarm:
        from sonarfix.core import service

        order = {"BLOCKER": 0, "CRITICAL": 1, "MAJOR": 2, "MINOR": 3, "INFO": 4}
        top = sorted(
            store.list_issues(args.project),
            key=lambda i: order.get(i.get("severity"), 9),
        )[: args.prewarm_count]
        print()
        print(f"  Analysing {len(top)} issue(s) ahead of the demo")
        for i in top:
            try:
                result = service.analyze_issue(i["id"])
                conf = (result.get("plan") or {}).get("confidence")
                shown = f"{conf:.0%}" if conf is not None else "-"
                print(f"    [OK]   {i['id']:<10} {i['rule']:<24} confidence {shown}")
            except Exception as exc:  # noqa: BLE001 - one failure must not stop the rest
                print(f"    [FAIL] {i['id']:<10} {exc}")

    print()
    print("  Start the demo:")
    print("    uvicorn sonarfix.api.main:app --reload --port 8000")
    print("    cd frontend && npm run dev")
    print("    http://localhost:5173")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
