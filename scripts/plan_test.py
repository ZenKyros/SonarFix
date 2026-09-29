"""Planner, recipes, batch runner and Bitbucket client - offline, zero tokens.

    python scripts/plan_test.py
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

WORKDIR = Path(tempfile.mkdtemp(prefix="sonarfix-plan-"))
REPO = WORKDIR / "repo"
os.environ["SONARFIX_DB"] = str(WORKDIR / "plan.db")
os.environ["SONAR_URL"] = "https://sonar.invalid"
os.environ["SONAR_TOKEN"] = "unused"
os.environ["BITBUCKET_TOKEN"] = "test-token"
# Set empty rather than popped: load_dotenv fills an absent name from .env,
# which would leak the developer's real Bitbucket settings into the test.
os.environ["BITBUCKET_USERNAME"] = ""
os.environ["BITBUCKET_URL"] = ""
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import httpx  # noqa: E402

from sonarfix.core import batch, engine, planner, scm, sonar, store  # noqa: E402
from sonarfix.core.recipes import Target, recipe_for  # noqa: E402

PROJECT = "plan-demo"
FILES = {
    "app/service.py": (
        "import logging\n"
        "logger = logging.getLogger(__name__)\n"
        "\n"
        "def load():\n"
        "    try:\n"
        "        return open('x').read()\n"
        "    except:\n"
        "        return None\n"
        "\n"
        "def save(data):\n"
        "    try:\n"
        "        write(data)\n"
        "    except Exception as e:\n"
        '        logger.error(f"save failed: {e}")\n'
    ),
    "web/App.jsx": (
        'import React, { useState, useCallback } from "react";\n'
        "import {\n"
        "  Zap,\n"
        "  Brain\n"
        '} from "lucide-react";\n'
        "export default function App() { return <Zap />; }\n"
    ),
    "src/Worker.cs": (
        "using System;\r\n"
        "using System.Text;\r\n"
        "\r\n"
        "class Worker\r\n"
        "{\r\n"
        "    void Run()\r\n"
        "    {\r\n"
        "        try { Step(); }\r\n"
        "        catch (Exception ex)\r\n"
        "        {\r\n"
        "            Log(ex);\r\n"
        "            throw ex;\r\n"
        "        }\r\n"
        "        // Step2(); Step3();\r\n"
        "    }\r\n"
        "}\r\n"
    ),
    "src/Stale.cs": "using System;\r\nclass Stale { }\r\n",
    "src/Complex.cs": "class Complex { void Big() { } }\n",
    "tools/venv/pyvenv.cfg": "home = /usr/bin\n",
    "tools/venv/bin/Activate.ps1": "Param(\n)\n",
}


def issue(key, rule, path, line, sl, el, so, eo, message, severity="MAJOR"):
    return {
        "key": key, "rule": rule, "severity": severity, "type": "CODE_SMELL",
        "status": "OPEN", "message": message, "component": f"{PROJECT}:{path}",
        "line": line,
        "textRange": {"startLine": sl, "endLine": el, "startOffset": so, "endOffset": eo},
    }


ISSUES = [
    issue("I-bare", "python:S5754", "app/service.py", 7, 7, 7, 4, 10, "Specify an exception class", "CRITICAL"),
    issue("I-log", "python:S8572", "app/service.py", 14, 14, 14, 15, 20, 'Use "logging.exception()" instead.'),
    issue("I-js1", "javascript:S1128", "web/App.jsx", 1, 1, 1, 26, 37, "Remove this unused import of 'useCallback'.", "MINOR"),
    issue("I-js2", "javascript:S1128", "web/App.jsx", 4, 4, 4, 2, 7, "Remove this unused import of 'Brain'.", "MINOR"),
    issue("I-using", "csharpsquid:S1128", "src/Worker.cs", 2, 2, 2, 0, 18, "Remove this unnecessary 'using'.", "MINOR"),
    issue("I-throw", "csharpsquid:S3445", "src/Worker.cs", 12, 12, 12, 12, 21, "Consider using 'throw;' to preserve the stack trace."),
    issue("I-cmt", "csharpsquid:S125", "src/Worker.cs", 14, 14, 14, 8, 30, "Remove this commented out code."),
    issue("I-dup", "csharpsquid:S125", "src/Worker.cs", 14, 14, 14, 8, 30, "Remove this commented out code."),
    # Sonar says line 2 is a using directive, but the clone moved on: stale.
    issue("I-stale", "csharpsquid:S1128", "src/Stale.cs", 2, 2, 2, 0, 14, "Remove this unnecessary 'using'.", "MINOR"),
    issue("I-cx", "csharpsquid:S3776", "src/Complex.cs", 1, 1, 1, 20, 23, "Refactor this method to reduce its Cognitive Complexity.", "CRITICAL"),
    issue("I-venv", "powershelldre:S8642", "tools/venv/bin/Activate.ps1", 1, 1, 1, 0, 5, "Change case of keyword"),
]

checks: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    checks.append((name, bool(ok), detail))


def git(*args: str) -> str:
    out = subprocess.run(["git", "-C", str(REPO), *args], capture_output=True, text=True)
    if out.returncode != 0:
        raise SystemExit(f"git {args} failed: {out.stderr}")
    return out.stdout


def build_repo() -> None:
    for rel, text in FILES.items():
        path = REPO / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text.encode("utf-8"))
    git("init", "-q", "-b", "main")
    git("config", "user.email", "t@example.com")
    git("config", "user.name", "Plan Test")
    git("config", "core.autocrlf", "false")
    git("add", "-A")
    git("commit", "-q", "-m", "init")
    git("remote", "add", "origin", "https://bitbucket.example.com/scm/DEMO/legacy-app.git")


def test_recipes() -> None:
    r = recipe_for("csharpsquid:S3445")
    out = r.apply(["        throw ex;\r\n"], Target(1, 1, 8, 17, ""))
    check("rethrow keeps CRLF", out == ["        throw;\r\n"], repr(out))
    r = recipe_for("csharpsquid:S1128")
    check("using statement is not a directive",
          r.apply(["using (var s = Open()) { }\n"], Target(1, 1, 0, 5, "")) is None)
    r = recipe_for("csharpsquid:S125")
    check("S125 refuses to delete real code",
          r.apply(["int a = 1;\n"], Target(1, 1, 0, 10, "")) is None)
    r = recipe_for("python:S8572")
    check("S8572 refuses a wrong token",
          r.apply(["logger.info(x)\n"], Target(1, 1, 7, 11, "")) is None)


def test_plan() -> dict:
    plan = planner.build_plan(PROJECT)
    s = plan["summary"]
    groups = {g["id"]: g for g in plan["groups"]}
    check("total issues", s["total"] == 11, str(s))
    check("duplicate detected", s["duplicates"] == 1, str(s))
    check("mechanical count", s["mechanical"] == 7, str(s))
    check("ai count (stale + complexity)", s["ai"] == 2, str(s))
    check("venv issue skipped", s["skip"] == 1 and "virtualenv" in (groups["skip:powershelldre:S8642"]["skip_reason"] or ""))
    check("stale issue routed to AI with a note",
          any(o["note"] for o in groups["ai:csharpsquid:S1128"]["occurrences"]))
    check("mechanical group has a clean example",
          groups["mechanical:python:S8572"]["recipe"]["after"].count("logger.exception") == 1)
    check("preview diff is real",
          "+        logger.exception" in groups["mechanical:python:S8572"]["example"]["diff"])
    return plan


def stub_fix(repo_path, task):
    path = Path(repo_path) / "src/Complex.cs"
    path.write_text("class Complex { void Big() { Small(); } void Small() { } }\n")
    stub_fix.task = task
    return {"changes_summary": "Extracted Small().", "files_changed": ["src/Complex.cs"],
            "testing_suggestions": "Run Worker tests.", "applied": True}


def test_batch() -> int:
    engine.run_fix = stub_fix
    sonar.get_rule = lambda key: {"key": key, "name": "Cognitive Complexity", "description": "Keep it simple."}

    try:
        batch.create(PROJECT, [], ["ai:csharpsquid:S3776"], approve_ai=False)
        check("AI requires explicit approval", False)
    except batch.BatchError:
        check("AI requires explicit approval", True)

    mech = ["mechanical:python:S5754", "mechanical:python:S8572",
            "mechanical:javascript:S1128", "mechanical:csharpsquid:S1128",
            "mechanical:csharpsquid:S3445", "mechanical:csharpsquid:S125"]
    batch_id = batch.create(PROJECT, mech, ["ai:csharpsquid:S3776"], approve_ai=True,
                            notes="Keep method names stable.")
    batch.run(batch_id)
    b = store.get_batch(batch_id)
    check("batch ready", b["status"] == "ready", f"{b['status']} {b.get('error')}")
    check("working tree back on base", git("rev-parse", "--abbrev-ref", "HEAD").strip() == "main")
    check("base untouched", "except:" in (REPO / "app/service.py").read_text())

    log = git("log", "--format=%s", f"main..{b['branch']}").splitlines()
    check("two commits: mechanical + ai", len(log) == 2, str(log))
    check("mechanical commit says no AI", any("mechanical" in m for m in log))

    show = lambda p: git("show", f"{b['branch']}:{p}")
    py = show("app/service.py")
    check("bare except fixed", "except Exception:" in py and "except:" not in py)
    check("logger.exception applied", 'logger.exception(f"save failed' in py)
    js = show("web/App.jsx")
    check("js specifier removed inline", 'import React, { useState } from "react";' in js, js)
    check("js specifier line removed", "Brain" not in js and "Zap," in js, js)
    cs = subprocess.run(["git", "-C", str(REPO), "show", f"{b['branch']}:src/Worker.cs"],
                        capture_output=True).stdout
    check("C# CRLF preserved", b"\r\n" in cs and b"\n" not in cs.replace(b"\r\n", b""))
    check("C# using removed", b"using System.Text;" not in cs and b"using System;" in cs)
    check("C# rethrow fixed", b"throw;" in cs and b"throw ex;" not in cs)
    check("C# commented code removed", b"// Step2" not in cs)

    check("AI task lists the occurrence", "/src/Complex.cs:1" in stub_fix.task)
    check("AI task carries reviewer notes", "Keep method names stable." in stub_fix.task)
    check("stale issue never touched", "using System;" in show("src/Stale.cs"))
    check("PR text written without an LLM", "SonarFix: resolve" in b["pr_title"]
          and "## Mechanical fixes (no AI)" in b["pr_description"]
          and "## AI fixes (human-approved)" in b["pr_description"])
    return batch_id


def test_bitbucket(batch_id: int) -> None:
    cloud = scm.parse_remote("https://someone@bitbucket.org/acme/legacy-app.git")
    check("cloud remote parsed", cloud and cloud.kind == "cloud" and cloud.label == "acme/legacy-app")
    scp = scm.parse_remote("git@bitbucket.org:acme/legacy-app.git")
    check("cloud ssh remote parsed", scp and scp.kind == "cloud")
    server = scm.parse_remote("https://bitbucket.example.com/context/scm/DEMO/legacy-app.git")
    check("server remote parsed", server and server.kind == "server"
          and server.api_base == "https://bitbucket.example.com/context" and server.owner == "DEMO")
    check("github is not bitbucket", scm.parse_remote("https://github.com/a/b.git") is None)
    check("status ready with token", scm.status(REPO)["ready"])

    sent = {}

    def handler(request: httpx.Request) -> httpx.Response:
        sent["url"] = str(request.url)
        sent["auth"] = request.headers.get("authorization")
        sent["body"] = json.loads(request.content)
        return httpx.Response(201, json={"links": {"self": [{"href": "https://bb/pr/7"}]}})

    real_client = httpx.Client
    scm.httpx.Client = lambda **kw: real_client(transport=httpx.MockTransport(handler))
    scm.push = lambda repo_path, branch: sent.setdefault("pushed", branch)
    try:
        result = batch.create_pull_request(batch_id)
    finally:
        scm.httpx.Client = real_client
    check("PR created", result["status"] == "pr_open" and result["pr_url"] == "https://bb/pr/7")
    check("server PR endpoint", sent["url"].endswith("/rest/api/1.0/projects/DEMO/repos/legacy-app/pull-requests"), sent.get("url"))
    check("bearer auth", sent["auth"] == "Bearer test-token")
    check("branch pushed first", sent.get("pushed") == result["branch"])
    check("PR targets base", sent["body"]["toRef"]["id"] == "refs/heads/main")


def main() -> int:
    build_repo()
    store.init_db()
    store.upsert_projects([{"key": PROJECT, "name": "Legacy App"}])
    store.set_repo_path(PROJECT, str(REPO))
    store.replace_issues(PROJECT, ISSUES)

    test_recipes()
    test_plan()
    batch_id = test_batch()
    test_bitbucket(batch_id)

    width = max(len(n) for n, _, _ in checks)
    failed = 0
    for name, ok, detail in checks:
        print(f"{'PASS' if ok else 'FAIL'}  {name.ljust(width)}  {'' if ok else detail}")
        failed += not ok
    print(f"\n{len(checks) - failed}/{len(checks)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
