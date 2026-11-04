"""Find the local clone for a project, wherever the user keeps it.

A project's clone path is stored per Sonar project key, but one git repo is
often scanned as several Sonar projects (monorepos, or the same repo scanned
under a different key). Binding a folder through one key must not leave the
others reporting "no local repository" - so a project with no path of its own
adopts the clone already bound to any project that points at the same remote.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from . import scm, sonar, store


def normalize_remote(url: str) -> str:
    """Compare remotes regardless of scheme, credentials, case or '.git'."""
    url = (url or "").strip().lower()
    url = re.sub(r"^[a-z+]+://", "", url)
    url = re.sub(r"^[^@/]+@", "", url)  # user[:token]@host
    url = url.replace(":", "/", 1) if re.match(r"^[^/]+:[^/\d]", url) else url  # scp-style ssh
    return url.removesuffix("/").removesuffix(".git")


def _is_clone(path: str | None) -> bool:
    return bool(path) and (Path(path) / ".git").exists()


def project_with_repo(project_key: str) -> dict[str, Any] | None:
    """The project row, with `repo_path` filled in from a sibling that shares
    its git remote when it has none of its own. Persists what it finds."""
    project = store.get_project(project_key)
    if not project or project.get("repo_path"):
        return project

    try:
        binding = sonar.get_repo_binding(project_key)
    except Exception:  # noqa: BLE001 - best effort; the caller reports the gap
        return project
    wanted = normalize_remote(sonar.clone_url_for_binding(binding) or "") if binding else ""
    if not wanted:
        return project

    for other in store.list_projects():
        path = other.get("repo_path")
        if other["key"] == project_key or not _is_clone(path):
            continue
        if normalize_remote(scm.origin_url(path)) == wanted:
            store.set_repo_path(project_key, path)
            return store.get_project(project_key)
    return project
