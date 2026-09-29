"""Remediation planning: group issues and route each to the cheapest safe fix.

Pure local work - no LLM, no network. For every open issue it decides:

    mechanical  a recipe matched the exact code Sonar flagged (0 tokens)
    ai          needs judgement; only runs after a human approves the group
    skip        vendored, generated or build-output code; fix the scan instead
"""

from __future__ import annotations

import difflib
import json
import re
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import store
from .recipes import Recipe, recipe_for, target_from
from .repo import RepoWorkspace

BUCKET_ORDER = {"mechanical": 0, "ai": 1, "skip": 2}

_VENDORED_DIRS = {
    "node_modules", "bower_components", "vendor", "third_party", "thirdparty",
    "3rdpartylibs", "site-packages", ".venv", "venv", "packages",
}
_BUILD_DIRS = {"bin", "obj", "dist", "build", "out", "target"}
_GENERATED_SUFFIXES = (
    ".designer.cs", ".g.cs", ".g.i.cs", ".generated.cs", ".min.js", ".min.css",
    ".pb.go", "_pb2.py",
)


# --- source files ------------------------------------------------------------


@dataclass
class SourceFile:
    """A file's lines plus how to write them back byte-for-byte compatible."""

    path: Path
    lines: list[str]
    encoding: str
    bom: bool

    @classmethod
    def load(cls, path: Path) -> "SourceFile":
        data = path.read_bytes()
        bom = data.startswith(b"\xef\xbb\xbf")
        try:
            text, encoding = data.decode("utf-8-sig"), "utf-8"
        except UnicodeDecodeError:
            # Legacy .NET/Java sources are often Windows-1252.
            text, encoding = data.decode("cp1252", errors="replace"), "cp1252"
        return cls(path, text.splitlines(keepends=True), encoding, bom)

    def save(self, lines: list[str]) -> None:
        data = "".join(lines).encode(self.encoding, errors="replace")
        self.path.write_bytes((b"\xef\xbb\xbf" if self.bom else b"") + data)


def unified_diff(path: str, before: list[str], after: list[str], context: int = 2) -> str:
    return "".join(
        difflib.unified_diff(
            [line.rstrip("\r\n") + "\n" for line in before],
            [line.rstrip("\r\n") + "\n" for line in after],
            fromfile=f"a/{path}",
            tofile=f"b/{path}",
            n=context,
        )
    )


# --- classification ----------------------------------------------------------


class _SkipDetector:
    def __init__(self, root: Path) -> None:
        self.root = root
        self._venv_cache: dict[Path, bool] = {}

    def _is_venv(self, directory: Path) -> bool:
        if directory not in self._venv_cache:
            self._venv_cache[directory] = (directory / "pyvenv.cfg").is_file()
        return self._venv_cache[directory]

    def reason(self, file_path: str) -> str | None:
        parts = Path(file_path).parts
        lowered = [p.lower() for p in parts]
        for i in range(1, len(parts)):
            if self._is_venv(self.root.joinpath(*parts[:i])):
                return f"Checked-in Python virtualenv ({'/'.join(parts[:i])})"
        for p in lowered[:-1]:
            if p in _VENDORED_DIRS:
                return f"Vendored dependency ({p}/)"
            if p in _BUILD_DIRS:
                return f"Build output ({p}/)"
        if lowered and lowered[-1].endswith(_GENERATED_SUFFIXES):
            return "Generated file"
        return None


_STRING_RE = re.compile(r"""(?:[rbuf]{0,2})("([^"\\]|\\.)*"|'([^'\\]|\\.)*')""", re.I)
_NUMBER_RE = re.compile(r"\b\d+(\.\d+)?\b")


def fingerprint(text: str) -> str:
    """Code shape: literals and whitespace erased, identifiers kept."""
    shape = _STRING_RE.sub("S", text)
    shape = _NUMBER_RE.sub("N", shape)
    return re.sub(r"\s+", "", shape)


@dataclass
class Occurrence:
    issue: dict[str, Any]
    raw: dict[str, Any]
    bucket: str
    shape: str = ""
    duplicate_of: str | None = None
    skip_reason: str | None = None
    recipe: Recipe | None = None
    diff: str | None = None
    note: str | None = None
    snippet: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "issue_id": self.issue["id"],
            "file": self.issue.get("file_path"),
            "line": self.issue.get("line"),
            "message": self.issue.get("message"),
            "severity": self.issue.get("severity"),
            "duplicate_of": self.duplicate_of,
            "diff": self.diff,
            "note": self.note,
            "snippet": self.snippet,
        }


@dataclass
class Group:
    bucket: str
    rule: str
    occurrences: list[Occurrence] = field(default_factory=list)

    @property
    def id(self) -> str:
        return f"{self.bucket}:{self.rule}"

    def as_dict(self) -> dict[str, Any]:
        primary = [o for o in self.occurrences if not o.duplicate_of]
        first = primary[0] if primary else self.occurrences[0]
        recipe = first.recipe
        worst = min(
            (o.issue.get("severity") for o in self.occurrences),
            key=lambda s: store.SEVERITY_RANK.get(s, 9),
        )
        return {
            "id": self.id,
            "bucket": self.bucket,
            "rule": self.rule,
            "severity": worst,
            "type": first.issue.get("type"),
            "message": first.issue.get("message"),
            "count": len(self.occurrences),
            "unique": len(primary),
            "duplicates": len(self.occurrences) - len(primary),
            "files": len({o.issue.get("file_path") for o in self.occurrences}),
            "shapes": len({o.shape for o in primary}),
            "skip_reason": first.skip_reason,
            "recipe": (
                {
                    "id": recipe.id,
                    "title": recipe.title,
                    "why_safe": recipe.why_safe,
                    "before": recipe.before,
                    "after": recipe.after,
                }
                if recipe and self.bucket == "mechanical"
                else None
            ),
            "example": first.as_dict(),
            "occurrences": [o.as_dict() for o in self.occurrences],
        }


def _snippet(lines: list[str], line: int | None, radius: int = 2) -> str | None:
    if not line or not lines:
        return None
    start, end = max(1, line - radius), min(len(lines), line + radius)
    width = len(str(end))
    return "".join(
        f"{n:>{width}}{'>' if n == line else ' '}| {lines[n - 1].rstrip()}\n"
        for n in range(start, end + 1)
    )


def classify(project_key: str) -> tuple[list[Occurrence], RepoWorkspace]:
    project = store.get_project(project_key)
    if not project:
        raise ValueError(f"Unknown project: {project_key}")
    if not project.get("repo_path"):
        raise ValueError(
            "Set the local repository path for this project first - the planner "
            "reads the flagged code to decide what can be fixed mechanically."
        )

    workspace = RepoWorkspace(project["repo_path"])
    skipper = _SkipDetector(workspace.path)
    files: dict[str, SourceFile | None] = {}
    seen: dict[tuple[str, str, int], str] = {}
    result: list[Occurrence] = []

    def source(file_path: str) -> SourceFile | None:
        if file_path not in files:
            try:
                path = workspace.resolve(file_path)
                files[file_path] = SourceFile.load(path) if path.is_file() else None
            except Exception:  # noqa: BLE001 - unreadable file: AI/human path
                files[file_path] = None
        return files[file_path]

    for issue in store.list_issues(project_key, include_raw=True):
        raw = json.loads(issue.get("raw_json") or "{}")
        file_path = issue.get("file_path") or ""
        occ = Occurrence(issue=issue, raw=raw, bucket="ai")

        src = source(file_path)
        target = target_from(issue, raw)
        if src:
            block = src.lines[target.start_line - 1 : target.end_line]
            occ.shape = fingerprint("".join(block))
            occ.snippet = _snippet(src.lines, issue.get("line"))

        key = (issue.get("rule") or "", file_path, target.start_line)
        if key in seen:
            occ.duplicate_of = seen[key]
        else:
            seen[key] = issue["id"]

        occ.skip_reason = skipper.reason(file_path)
        if occ.skip_reason:
            occ.bucket = "skip"
        elif src is None:
            occ.note = "File not found in the local clone - is it on the scanned branch?"
        else:
            recipe = recipe_for(issue.get("rule"))
            if recipe:
                after = recipe.apply(src.lines, target)
                if after is not None:
                    occ.bucket, occ.recipe = "mechanical", recipe
                    occ.diff = unified_diff(file_path, src.lines, after)
                else:
                    occ.note = (
                        "A recipe exists for this rule, but the code at the reported "
                        "location did not match - the clone may differ from the scan."
                    )
        result.append(occ)

    # A duplicate follows its primary, so both land in the same group.
    by_id = {o.issue["id"]: o for o in result}
    for occ in result:
        if occ.duplicate_of:
            primary = by_id[occ.duplicate_of]
            occ.bucket, occ.recipe, occ.skip_reason = (
                primary.bucket, primary.recipe, primary.skip_reason
            )
            occ.diff = None
    return result, workspace


def build_groups(occurrences: list[Occurrence]) -> list[Group]:
    groups: dict[tuple[str, str], Group] = {}
    for occ in occurrences:
        rule = occ.issue.get("rule") or "unknown"
        group = groups.setdefault((occ.bucket, rule), Group(occ.bucket, rule))
        group.occurrences.append(occ)
    return sorted(
        groups.values(),
        key=lambda g: (
            BUCKET_ORDER[g.bucket],
            min(store.SEVERITY_RANK.get(o.issue.get("severity"), 9) for o in g.occurrences),
            -len(g.occurrences),
        ),
    )


def build_plan(project_key: str) -> dict[str, Any]:
    occurrences, _ = classify(project_key)
    groups = build_groups(occurrences)
    counts = defaultdict(int)
    for occ in occurrences:
        if not occ.duplicate_of:
            counts[occ.bucket] += 1
    project = store.get_project(project_key) or {}
    return {
        "project": {"key": project_key, "name": project.get("name") or project_key},
        "summary": {
            "total": len(occurrences),
            "duplicates": sum(1 for o in occurrences if o.duplicate_of),
            "groups": len(groups),
            "mechanical": counts["mechanical"],
            "ai": counts["ai"],
            "skip": counts["skip"],
            "ai_groups": sum(1 for g in groups if g.bucket == "ai"),
        },
        "groups": [g.as_dict() for g in groups],
    }
