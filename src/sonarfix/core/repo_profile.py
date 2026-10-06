"""Facts about the project that owns a file, from the PowerShell discovery run.

`.sonarfix/runs/<run>/01-RepoDiscoveryAgent.json` and `03-DependencyGraphAgent.json`
already know each project's target framework, style and how many projects
depend on it. Handing that to the model keeps it from proposing C# a .NET 3.5
project cannot compile, and tells it when a change is widely consumed.

Everything here is optional: with no run on disk, `profile_for` returns "".
"""

from __future__ import annotations

import json
import os
from functools import lru_cache
from pathlib import Path

_DISCOVERY = "01-RepoDiscoveryAgent.json"
_GRAPH = "03-DependencyGraphAgent.json"
_WIDE_USE = 20  # consumers above this: treat public surface as frozen


def _runs_dir() -> Path:
    override = os.environ.get("SONARFIX_RUNS_DIR")
    if override:
        return Path(override)
    return Path(__file__).resolve().parents[3] / ".sonarfix" / "runs"


def _latest_run() -> Path | None:
    root = _runs_dir()
    if not root.is_dir():
        return None
    runs = [d for d in root.iterdir() if (d / _DISCOVERY).is_file()]
    return max(runs, key=lambda d: (d / _DISCOVERY).stat().st_mtime, default=None)


@lru_cache(maxsize=4)
def _load(run: str, mtime: float) -> tuple[list[dict], dict[str, dict]]:
    """(projects, graph nodes by path) for one run. `mtime` keys the cache."""
    base = Path(run)
    projects = json.loads((base / _DISCOVERY).read_text(encoding="utf-8-sig"))["data"]["projects"]
    nodes: dict[str, dict] = {}
    graph = base / _GRAPH
    if graph.is_file():
        for node in json.loads(graph.read_text(encoding="utf-8-sig"))["data"]["nodes"]:
            nodes[node["path"]] = node
    return projects, nodes


def _owner(projects: list[dict], file_path: str) -> dict | None:
    """The project whose folder is the deepest prefix of `file_path`."""
    target = file_path.replace("\\", "/").lstrip("/")
    best, best_len = None, -1
    for project in projects:
        folder = project["path"].rsplit("/", 1)[0] + "/" if "/" in project["path"] else ""
        if target.startswith(folder) and len(folder) > best_len:
            best, best_len = project, len(folder)
    return best


def profile_for(file_path: str) -> str:
    """A short markdown block describing the project that owns `file_path`."""
    try:
        run = _latest_run()
        if not run:
            return ""
        projects, nodes = _load(str(run), (run / _DISCOVERY).stat().st_mtime)
        project = _owner(projects, file_path)
    except (OSError, ValueError, KeyError):
        return ""  # a missing or malformed run must never block an analysis
    if not project:
        return ""

    node = nodes.get(project["path"], {})
    used_by = int(node.get("impactedCount") or 0)
    style = project.get("style") or "unknown"
    framework = project.get("targetFramework") or "unknown"

    lines = [
        f"- Project: {project['name']} (`{project['path']}`)",
        f"- Target framework: {framework}, {style} project format",
        f"- Test project: {'yes' if project.get('isTest') else 'no'}",
        f"- Projects that depend on it (directly or not): {used_by}",
    ]
    if style == "legacy":
        lines.append(
            "- Legacy .NET Framework project: use only language and library features "
            "this project already uses, and do not edit the .csproj."
        )
    if used_by > _WIDE_USE:
        lines.append(
            f"- Widely used ({used_by} consumers): do not change public signatures, "
            "exception types or behaviour."
        )
    return "\n".join(lines)
