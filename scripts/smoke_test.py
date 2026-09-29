"""End-to-end check of the workflow with the two agents stubbed out.

Exercises the real LangGraph run, the approval interrupt and resume, the git
branch/diff/commit path and every SQLite write - without calling SonarQube or
Claude, so it costs nothing and needs no credentials.

    python scripts/smoke_test.py
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

WORKDIR = Path(tempfile.mkdtemp(prefix="sonarfix-smoke-"))
REPO = WORKDIR / "repo"

# Point config at a throwaway database before anything reads the environment.
os.environ["SONARFIX_DB"] = str(WORKDIR / "smoke.db")
os.environ.setdefault("SONAR_URL", "https://sonar.invalid")
os.environ.setdefault("SONAR_TOKEN", "unused")
os.environ.setdefault("ANTHROPIC_API_KEY", "unused")

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sonarfix.core import graph, service, store  # noqa: E402
from sonarfix.core.agents import FixOutcome, IssueAnalysis, PullRequestText  # noqa: E402

PROJECT_KEY = "smoke-project"
ISSUE_ID = "AYsmokeIssue000001"
FILE_PATH = "src/main/java/Greeter.java"

ORIGINAL = """package demo;

public class Greeter {
    public String greet(String name) {
        if (name == "world") {
            return "hello world";
        }
        return "hello " + name;
    }
}
"""

FIXED = ORIGINAL.replace('name == "world"', '"world".equals(name)')


def git(*args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(REPO), *args], capture_output=True, text=True
    )
    if result.returncode != 0:
        raise SystemExit(f"git {' '.join(args)} failed: {result.stderr}")
    return result.stdout


def build_repo() -> None:
    (REPO / "src/main/java").mkdir(parents=True)
    (REPO / FILE_PATH).write_text(ORIGINAL, encoding="utf-8")
    git("init", "-q")
    git("config", "user.email", "smoke@example.com")
    git("config", "user.name", "Smoke Test")
    git("add", "-A")
    git("commit", "-q", "-m", "initial")


# --- stubs -------------------------------------------------------------------


def stub_analysis(_repo_path, _task):
    """Stands in for engine.run_analysis, whichever engine is configured."""
    return IssueAnalysis(
        explanation="String comparison uses == instead of .equals().",
        root_cause="Reference equality is used on a String literal.",
        impact="greet() takes the wrong branch for equal-but-distinct strings.",
        severity_assessment="Sonar's MAJOR is right; this is a real defect.",
        related_files=[FILE_PATH],
        remediation_plan=[
            f'In {FILE_PATH}, replace `name == "world"` with `"world".equals(name)`.'
        ],
        testing_notes='Call greet(new String("world")) and assert the branch.',
        confidence=0.92,
    ).model_dump()


def stub_fix(_repo_path, _task):
    """Stands in for engine.run_fix; writes the file like a real agent would."""
    (REPO / FILE_PATH).write_text(FIXED, encoding="utf-8")
    return FixOutcome(
        changes_summary="Replaced == with .equals() in Greeter.greet().",
        files_changed=[FILE_PATH],
        testing_suggestions='Add a unit test using new String("world").',
        applied=True,
    ).model_dump()


def stub_pr_text(_repo_path, _task):
    """Stands in for engine.run_pr_text."""
    return PullRequestText(
        commit_message="fix: compare strings with equals in Greeter\n\n"
        "Reference equality gave the wrong branch.",
        pr_title="Fix string comparison in Greeter",
        pr_description="Replaces `==` with `.equals()`.",
    ).model_dump()


def install_stubs() -> None:
    # Patch at the engine seam, so this covers whichever engine is configured.
    graph.engine.run_analysis = stub_analysis
    graph.engine.run_fix = stub_fix
    graph.engine.run_pr_text = stub_pr_text
    graph.sonar.get_rule = lambda key: {
        "key": key,
        "name": "Strings should be compared with equals()",
        "description": "Using == on String compares references, not content.",
    }


# --- assertions --------------------------------------------------------------

checks: list[tuple[str, bool, str]] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    checks.append((name, bool(condition), detail))


def main() -> int:
    build_repo()
    install_stubs()

    store.init_db()
    store.upsert_projects([{"key": PROJECT_KEY, "name": "Smoke Project"}])
    service.set_repo_path(PROJECT_KEY, str(REPO))
    store.replace_issues(
        PROJECT_KEY,
        [
            {
                "key": ISSUE_ID,
                "rule": "java:S1698",
                "severity": "MAJOR",
                "type": "BUG",
                "status": "OPEN",
                "message": "Strings should be compared with equals()",
                "component": f"{PROJECT_KEY}:{FILE_PATH}",
                "line": 5,
            }
        ],
    )

    detail = service.issue_detail(ISSUE_ID)
    check("issue detail resolves the file", detail["code_context"] is not None)
    check(
        "code context centres on the reported line",
        "name ==" in (detail["code_context"] or {}).get("text", ""),
    )

    analysis = service.analyze_issue(ISSUE_ID)
    check("analysis parks on the approval interrupt", analysis["awaiting_approval"])
    check("plan persisted", bool(analysis["plan"] and analysis["plan"]["id"]))
    check(
        "plan has remediation steps",
        bool((analysis["plan"] or {}).get("remediation_plan")),
    )
    check(
        "plan status is pending before approval",
        (analysis["plan"] or {}).get("status") == "pending",
    )

    resumed = service.workflow_state(ISSUE_ID)
    check("state survives a fresh read (checkpointed)", resumed["awaiting_approval"])

    result = service.decide(ISSUE_ID, approved=True, feedback="Keep the diff minimal.")
    check("fix applied", result["status"] == "applied", str(result.get("error")))
    check("branch created", str(result.get("branch", "")).startswith("sonarfix/"))
    check("diff captured", ".equals(name)" in (result.get("diff") or ""))
    check("commit made", bool(result.get("commit_sha")))
    check("commit message generated", bool((result.get("pr") or {}).get("commit_message")))
    check("run row persisted", (result.get("run") or {}).get("status") == "applied")
    check(
        "plan marked approved",
        (result.get("plan") or {}).get("status") == "approved",
    )

    check("working tree is clean after commit", not git("status", "--porcelain").strip())
    check("fix is on disk", '"world".equals(name)' in (REPO / FILE_PATH).read_text())
    check("branch is checked out", "sonarfix/" in git("rev-parse", "--abbrev-ref", "HEAD"))

    # Rejection path, on a clean run.
    service.analyze_issue(ISSUE_ID)
    rejected = service.decide(ISSUE_ID, approved=False)
    check("rejection short-circuits", rejected["status"] == "rejected")
    check("no branch on rejection", not rejected.get("branch"))
    check(
        "plan marked rejected",
        (rejected.get("plan") or {}).get("status") == "rejected",
    )

    width = max(len(name) for name, _, _ in checks)
    failed = 0
    for name, passed, detail in checks:
        print(f"{'PASS' if passed else 'FAIL'}  {name.ljust(width)}  {detail if not passed else ''}")
        failed += not passed

    print(f"\n{len(checks) - failed}/{len(checks)} checks passed")
    print(f"Scratch repo: {REPO}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
