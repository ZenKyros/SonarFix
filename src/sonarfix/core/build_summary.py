"""Explain a failed build in plain words.

Compiler output is long and repetitive (MSBuild prints the same error once per
project that hits it). This reads it, groups the errors by what they mean,
and works out the thing a reviewer actually needs to know: did the fix break
the build, or was the project unbuildable on this machine to begin with?
"""

from __future__ import annotations

import re
from collections import OrderedDict
from typing import Any

# path(line,col): error CS0246: message [project.csproj]
_MSBUILD_ERROR = re.compile(
    r"^(?P<file>.+?)\((?P<line>\d+)(?:,\d+)?\):\s*(?:\w+\s+)?error\s+(?P<code>[A-Za-z]+\d+):\s*(?P<msg>.*?)(?:\s*\[[^\]]+\])?\s*$"
)
# error NU1101: message   /   MSBUILD : error MSB1009: message   (no file position)
_BARE_ERROR = re.compile(r"error\s+(?P<code>[A-Za-z]+\d+):\s*(?P<msg>.*?)(?:\s*\[[^\]]+\])?\s*$")
_MISSING_NAME = re.compile(r"(?:type or namespace name|name) '([^']+)' (?:could not be found|does not exist)")


# code prefix/exact -> (category, title, plain-English explanation)
_RULES: list[tuple[re.Pattern[str], str, str, str]] = [
    (re.compile(r"^(CS0246|CS0234|CS0012|CS0400)$"), "missing-ref",
     "A library or namespace could not be found",
     "The code uses a library (a NuGet package or another project's DLL) that was not available to the compiler."),
    (re.compile(r"^(NU1\d+)$"), "restore",
     "NuGet packages could not be downloaded",
     "The build could not restore the packages it depends on - usually a network, VPN or package-source problem."),
    (re.compile(r"^(MSB3243|MSB3245|MSB3246|MSB3277|MSB3270)$"), "missing-ref",
     "A referenced DLL could not be resolved",
     "The project points at a DLL that is missing on this machine or has a conflicting version."),
    (re.compile(r"^(MSB4019|MSB4057|MSB4226|MSB1009|MSB3202)$"), "tooling",
     "The build tooling or a project file is missing",
     "MSBuild could not find a project file, target, or Visual Studio component the project needs."),
    (re.compile(r"^(CS0103|CS0117|CS1061|CS0029|CS0030|CS0266|CS1503|CS1501|CS0128|CS0136|CS0111|CS0165|CS0161|CS0120|CS0122|CS0535|CS0115|CS0102|CS0426)$"), "code",
     "The code does not compile",
     "A name, type or call in the changed code is not valid where it is used."),
    (re.compile(r"^(CS1002|CS1003|CS1026|CS1513|CS1514|CS1519|CS1525|CS1597|CS8641)$"), "syntax",
     "A syntax error in the code",
     "The code has a typo-level mistake such as a missing semicolon or bracket."),
]
_FALLBACK = ("other", "The build reported errors", "The build stopped because of the errors listed below.")


def _classify(code: str) -> tuple[str, str, str]:
    for pattern, category, title, plain in _RULES:
        if pattern.match(code):
            return category, title, plain
    return _FALLBACK


def _norm(path: str) -> str:
    return path.replace("\\", "/").lower().strip()


def _in_changed(error_file: str, changed: list[str]) -> bool:
    target = _norm(error_file)
    return any(target.endswith(_norm(c)) for c in changed if c)


def parse_errors(output: str) -> list[dict[str, Any]]:
    """Unique errors in the order first seen."""
    seen: "OrderedDict[tuple, dict[str, Any]]" = OrderedDict()
    for raw in (output or "").splitlines():
        line = raw.strip()
        match = _MSBUILD_ERROR.match(line)
        if match:
            item = {
                "file": match["file"], "line": int(match["line"]),
                "code": match["code"].upper(), "message": match["msg"].strip(),
            }
        else:
            match = _BARE_ERROR.search(line)
            if not match:
                continue
            item = {"file": None, "line": None, "code": match["code"].upper(), "message": match["msg"].strip()}
        seen.setdefault((item["file"], item["line"], item["code"], item["message"]), item)
    return list(seen.values())


_NEXT_STEPS = {
    "missing-ref": "Make sure the project's NuGet packages restore (connect to the VPN) and that any project it depends on builds first. SonarFix tries both automatically; if it still fails, open the solution once in Visual Studio, build it, then retry.",
    "restore": "Connect to the VPN or corporate network, check the NuGet package source is reachable, then retry.",
    "tooling": "Install the missing Visual Studio / .NET build component or check the project path, then retry.",
    "code": "This is a problem in the code change itself. Ask the AI to retry with the error above, or edit the change by hand.",
    "syntax": "This is a problem in the code change itself. Ask the AI to retry, or fix the typo by hand.",
    "other": "Read the first error below and the build log for details.",
}


def summarize(
    build_output: str | None, changed_files: list[str] | None = None, status: str | None = "failed"
) -> dict[str, Any] | None:
    """A reviewer-friendly explanation of a failed build, or None if the
    build did not fail or its output had no recognisable errors."""
    if status != "failed":
        return None
    changed = changed_files or []
    errors = parse_errors(build_output or "")
    if not errors:
        return None

    groups: "OrderedDict[str, dict[str, Any]]" = OrderedDict()
    for err in errors:
        category, title, plain = _classify(err["code"])
        key = f"{category}:{title}"
        group = groups.setdefault(key, {
            "category": category, "title": title, "plain": plain,
            "count": 0, "codes": [], "names": [], "examples": [], "in_changed": 0,
        })
        group["count"] += 1
        if err["code"] not in group["codes"]:
            group["codes"].append(err["code"])
        name = _MISSING_NAME.search(err["message"])
        if name and name.group(1) not in group["names"]:
            group["names"].append(name.group(1))
        if err["file"] and _in_changed(err["file"], changed):
            group["in_changed"] += 1
        if len(group["examples"]) < 4:
            group["examples"].append(
                {"file": err["file"], "line": err["line"], "code": err["code"], "message": err["message"]}
            )

    reasons = sorted(groups.values(), key=lambda g: -g["count"])
    environment = {"missing-ref", "restore", "tooling"}
    env_errors = sum(g["count"] for g in reasons if g["category"] in environment)
    code_errors = len(errors) - env_errors
    changed_hits = sum(g["in_changed"] for g in reasons if g["category"] not in environment)
    # Missing-library errors that hit files the fix never touched mean the
    # project was already unbuildable here - the fix is not the culprit.
    untouched_env = sum(
        g["count"] - g["in_changed"] for g in reasons if g["category"] in environment
    )

    if code_errors == 0 and (untouched_env > 0 or not changed):
        cause, label = "environment", "Probably not caused by this fix"
        names = [n for g in reasons if g["category"] in environment for n in g["names"]][:4]
        what = f" ({', '.join(names)})" if names else ""
        headline = (
            f"The project could not be built on this machine because required libraries{what} "
            "were not found. The code change itself does not appear to be the problem."
        )
    elif env_errors == 0 or changed_hits > 0:
        cause, label = "fix", "Caused by the code change"
        headline = f"The code change introduced {code_errors} compile error{'s' if code_errors != 1 else ''}."
    else:
        cause, label = "mixed", "Both a setup problem and code errors"
        headline = "The build has missing-library problems on this machine and some code errors."

    steps: list[str] = []
    for g in reasons:
        step = _NEXT_STEPS.get(g["category"])
        if step and step not in steps:
            steps.append(step)

    return {
        "cause": cause,
        "cause_label": label,
        "headline": headline,
        "total_errors": len(errors),
        "reasons": reasons,
        "next_steps": steps,
    }


def for_run(row: dict[str, Any]) -> dict[str, Any] | None:
    """`summarize` for a stored batch/run row (uses its diff to know which
    files the fix actually touched)."""
    from .report_diff import parse_diff

    changed = [f.path for f in parse_diff(row.get("diff") or "")]
    return summarize(row.get("build_output"), changed, row.get("build_status"))
