"""Local git repository access.

The agent edits files through its own filesystem tools; this module only owns
the git side - branching, diffing and committing - plus the code slice shown
in the UI.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

_SLUG_RE = re.compile(r"[^a-zA-Z0-9._-]+")


class RepoError(RuntimeError):
    """A git operation failed or the repository is not usable."""


def slugify(value: str, limit: int = 40) -> str:
    return _SLUG_RE.sub("-", value).strip("-").lower()[:limit] or "issue"


class RepoWorkspace:
    """A local clone of the project under analysis."""

    def __init__(self, repo_path: str | Path) -> None:
        self.path = Path(repo_path).expanduser().resolve()
        if not self.path.is_dir():
            raise RepoError(f"Repository path does not exist: {self.path}")
        if not (self.path / ".git").exists():
            raise RepoError(f"Not a git repository: {self.path}")

    # --- git ----------------------------------------------------------------

    def _git(self, *args: str) -> str:
        result = subprocess.run(
            ["git", "-C", str(self.path), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        if result.returncode != 0:
            raise RepoError(
                f"git {' '.join(args)} failed: {result.stderr.strip() or result.stdout.strip()}"
            )
        return result.stdout

    def current_branch(self) -> str:
        return self._git("rev-parse", "--abbrev-ref", "HEAD").strip()

    def branch_exists(self, name: str) -> bool:
        result = subprocess.run(
            ["git", "-C", str(self.path), "rev-parse", "--verify", name],
            capture_output=True,
            text=True,
        )
        return result.returncode == 0

    def is_dirty(self) -> bool:
        """True when the working tree has any change, tracked or untracked.

        The fix agent commits through us, so we only ever start from a clean
        tree - that way everything we stage afterwards is the agent's work.
        """
        return bool(self._git("status", "--porcelain", "-uall").strip())

    def start_branch(self, preferred_name: str) -> str:
        """Create and check out a fresh branch, de-duplicating the name."""
        name = preferred_name
        suffix = 2
        while self.branch_exists(name):
            name = f"{preferred_name}-{suffix}"
            suffix += 1
        self._git("checkout", "-b", name)
        return name

    def stage_all(self) -> None:
        """Stage everything the agent touched, including files it created.

        Staging first is what makes a brand-new test file show up in the diff -
        `git diff` alone ignores untracked files.
        """
        self._git("add", "-A")

    def staged_diff(self) -> str:
        return self._git("diff", "--cached")

    def staged_files(self) -> list[str]:
        """Repository-relative paths of the staged changes."""
        output = self._git("diff", "--cached", "--name-only")
        return [line.strip() for line in output.splitlines() if line.strip()]

    def commit(self, message: str) -> str:
        """Commit what is staged. Returns the short SHA."""
        self._git("commit", "-m", message)
        return self._git("rev-parse", "--short", "HEAD").strip()

    def checkout(self, branch: str) -> None:
        self._git("checkout", branch)

    def discard_changes(self) -> None:
        """Drop uncommitted edits, keeping the commits already on this branch."""
        self._git("reset", "--hard")
        self._git("clean", "-fd")

    def diff_between(self, base: str, branch: str) -> str:
        return self._git("diff", f"{base}...{branch}")

    def abandon(self, base_branch: str, branch: str | None = None) -> None:
        """Throw away a failed attempt and go back to where we started."""
        self._git("reset", "--hard")
        self._git("clean", "-fd")
        self._git("checkout", "--force", base_branch)
        if branch and branch != base_branch and self.branch_exists(branch):
            self._git("branch", "-D", branch)

    # --- reading ------------------------------------------------------------

    def resolve(self, file_path: str) -> Path:
        target = (self.path / file_path).resolve()
        if not str(target).startswith(str(self.path)):
            raise RepoError(f"Path escapes the repository: {file_path}")
        return target

    def exists(self, file_path: str) -> bool:
        try:
            return self.resolve(file_path).is_file()
        except RepoError:
            return False

    def read_file(self, file_path: str) -> str:
        target = self.resolve(file_path)
        if not target.is_file():
            raise RepoError(f"File not found in repository: {file_path}")
        return target.read_text(encoding="utf-8", errors="replace")

    def line_count(self, file_path: str) -> int:
        return len(self.read_file(file_path).splitlines())

    def context_slice(
        self, file_path: str, line: int | None, radius: int = 40
    ) -> dict[str, object]:
        """Numbered code window around the offending line, for prompts and UI."""
        lines = self.read_file(file_path).splitlines()
        if not line or line < 1:
            start, end = 1, min(len(lines), radius * 2)
        else:
            start = max(1, line - radius)
            end = min(len(lines), line + radius)

        width = len(str(end))
        rendered = "\n".join(
            f"{number:>{width}} | {lines[number - 1]}"
            for number in range(start, end + 1)
        )
        return {
            "text": rendered,
            "start_line": start,
            "end_line": end,
            "total_lines": len(lines),
        }
