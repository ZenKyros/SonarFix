"""Local build + test verification: does the fix compile, and do tests still pass,
before we propose a PR.

Finds the project (a .csproj/.vbproj for C#/.NET, a pom.xml for Maven, or a
build.gradle(.kts) for Gradle) owning each file the fix touched, and builds
(then tests) just those - the "blast radius".
"""

from __future__ import annotations

import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

from .config import get_settings

# Project marker file -> project kind, checked nearest-first while walking up
# from a changed file. .NET is checked before Java markers purely because it
# was the first language supported here; a directory should only ever have
# one kind of marker anyway.
_DOTNET_GLOBS = ("*.csproj", "*.vbproj")
_MAVEN_MARKER = "pom.xml"
_GRADLE_MARKERS = ("build.gradle", "build.gradle.kts")


def _project_for(repo_root: Path, file_path: str) -> tuple[str, Path] | None:
    """Nearest project file above `file_path`, as (kind, path), if any."""
    current = (repo_root / file_path).resolve().parent
    while True:
        dotnet_matches = []
        for pattern in _DOTNET_GLOBS:
            dotnet_matches.extend(sorted(current.glob(pattern)))
        if dotnet_matches:
            return "dotnet", dotnet_matches[0]
        maven = current / _MAVEN_MARKER
        if maven.is_file():
            return "maven", maven
        for name in _GRADLE_MARKERS:
            gradle = current / name
            if gradle.is_file():
                return "gradle", gradle
        if current == repo_root or current.parent == current:
            return None
        current = current.parent


def _find_msbuild() -> str | None:
    for name in ("msbuild", "MSBuild.exe"):
        found = shutil.which(name)
        if found:
            return found
    vswhere = Path(r"C:\Program Files (x86)\Microsoft Visual Studio\Installer\vswhere.exe")
    if not vswhere.is_file():
        return None
    result = subprocess.run(
        [
            str(vswhere), "-latest", "-requires", "Microsoft.Component.MSBuild",
            "-find", r"MSBuild\**\Bin\MSBuild.exe",
        ],
        capture_output=True, text=True,
    )
    candidates = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    return candidates[0] if candidates else None


def _is_sdk_style(csproj: Path) -> bool:
    """SDK-style projects (<Project Sdk="...">) build with `dotnet build`.

    Legacy .NET Framework projects (ToolsVersion-based, no Sdk attribute)
    only build with full MSBuild - the dotnet CLI rejects their format
    outright, which is not a code problem and must not be reported as one.
    """
    try:
        head = csproj.read_text(encoding="utf-8-sig", errors="replace")[:500]
    except OSError:
        return False
    return "Sdk=" in head


def _wrapper(root_for_marker: Path, repo_root: Path, names: tuple[str, str]) -> str | None:
    """A project's own wrapper script (mvnw/gradlew), searched from the marker's
    directory up to the repo root - wrappers pin a known-good tool version, so
    they are always preferred over whatever happens to be on PATH."""
    unix_name, windows_name = names
    current = root_for_marker
    while True:
        for name in (windows_name, unix_name):
            candidate = current / name
            if candidate.is_file():
                return str(candidate)
        if current == repo_root or current.parent == current:
            return None
        current = current.parent


class _Commands:
    """What to run to build and test one project, or None if the tool is missing."""

    def __init__(self, build: list[str] | None, test: list[str] | None, note: str = "") -> None:
        self.build = build
        self.test = test
        self.note = note


def _dotnet_commands(csproj: Path) -> _Commands:
    dotnet = shutil.which("dotnet")
    msbuild = _find_msbuild()
    if _is_sdk_style(csproj) and dotnet:
        return _Commands(
            build=[dotnet, "build", str(csproj), "--nologo", "-v", "minimal"],
            test=[dotnet, "test", str(csproj), "--nologo", "-v", "minimal"],
        )
    if msbuild:
        return _Commands(build=[msbuild, str(csproj), "/nologo", "/v:minimal"], test=None)
    if dotnet:
        return _Commands(
            build=[dotnet, "build", str(csproj), "--nologo", "-v", "minimal"],
            test=[dotnet, "test", str(csproj), "--nologo", "-v", "minimal"],
        )
    return _Commands(build=None, test=None, note="Neither 'dotnet' nor MSBuild.exe is on PATH.")


def _maven_commands(pom: Path, repo_root: Path) -> _Commands:
    tool = _wrapper(pom.parent, repo_root, ("mvnw", "mvnw.cmd")) or shutil.which("mvn")
    if not tool:
        return _Commands(build=None, test=None, note="Neither a Maven wrapper nor 'mvn' is on PATH.")
    return _Commands(
        build=[tool, "-q", "-f", str(pom), "-DskipTests", "compile"],
        test=[tool, "-q", "-f", str(pom), "test"],
    )


def _gradle_commands(marker: Path, repo_root: Path) -> _Commands:
    tool = _wrapper(marker.parent, repo_root, ("gradlew", "gradlew.bat")) or shutil.which("gradle")
    if not tool:
        return _Commands(build=None, test=None, note="Neither a Gradle wrapper nor 'gradle' is on PATH.")
    project_dir = marker.parent
    return _Commands(
        build=[tool, "-q", "-p", str(project_dir), "build", "-x", "test"],
        test=[tool, "-q", "-p", str(project_dir), "test"],
    )


def _commands_for(kind: str, marker: Path, repo_root: Path) -> _Commands:
    if kind == "dotnet":
        return _dotnet_commands(marker)
    if kind == "maven":
        return _maven_commands(marker, repo_root)
    return _gradle_commands(marker, repo_root)


def _unsupported_note(unsupported: list[str]) -> str:
    return (
        "Build verification covers C#/.NET, Maven and Gradle projects. "
        f"{len(unsupported)} changed file(s) are in another project type and were "
        f"not built: {', '.join(unsupported)}"
    )


def _run(tool_args: list[str], root: Path, timeout: int) -> tuple[bool, str, bool]:
    """Run one command; returns (ok, log_text, timed_out)."""
    start = time.monotonic()
    try:
        result = subprocess.run(
            tool_args, cwd=str(root), capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return False, f"Timed out after {timeout}s.", True

    duration = time.monotonic() - start
    tail = (result.stdout or "")[-4000:]
    if result.stderr:
        tail += "\n" + result.stderr[-2000:]
    log = f"({duration:.1f}s)\n{tail.strip()}"
    return result.returncode == 0, log, False


def verify(repo_path: str, changed_files: list[str]) -> dict[str, Any]:
    """Build, then run tests for, every project the changed files belong to.

    Never blocks the fix from being offered as a PR - purely informational.
    Files not owned by a recognised project file are reported back as
    `unsupported_files` instead of being silently ignored.

    Returns {"status": "passed"|"failed"|"skipped", "success": bool,
    "output": str, "projects": [str], "unsupported_files": [str],
    "test_status": "passed"|"failed"|"skipped"|None, "test_output": str}
    - shaped so it can be stored and shown to the user directly. `test_status`
    is None when no project in the blast radius has a runnable test command
    (e.g. legacy .NET Framework projects, which only build via MSBuild).
    """
    settings = get_settings()
    empty = {
        "status": "skipped", "success": True, "output": "",
        "projects": [], "unsupported_files": [],
        "test_status": None, "test_output": "",
    }
    if not settings.build_enabled:
        empty["output"] = "Build verification is disabled (SONARFIX_BUILD_ENABLED=false)."
        return empty

    root = Path(repo_path).resolve()
    projects: dict[str, tuple[str, Path]] = {}
    unsupported: list[str] = []
    for file_path in changed_files:
        found = _project_for(root, file_path)
        if found:
            kind, marker = found
            projects[str(marker)] = (kind, marker)
        else:
            unsupported.append(file_path)

    note = _unsupported_note(unsupported) if unsupported else ""

    if not projects:
        output = note or "No recognised project file owns the changed files; nothing to build."
        empty["output"] = output
        empty["unsupported_files"] = unsupported
        return empty

    build_logs: list[str] = []
    test_logs: list[str] = []
    any_test_ran = False
    build_failed = False
    test_failed = False

    for kind, marker in projects.values():
        rel = marker.relative_to(root)
        commands = _commands_for(kind, marker, root)

        if not commands.build:
            build_logs.append(f"### {rel}\n{commands.note}")
            build_failed = True
            continue

        ok, log, timed_out = _run(commands.build, root, settings.build_timeout)
        build_logs.append(f"### {rel} [build]{' TIMEOUT' if timed_out else ''}\n{log}")
        if not ok:
            build_failed = True
            continue  # don't test code that doesn't even compile

        if commands.test:
            any_test_ran = True
            test_ok, test_log, test_timed_out = _run(commands.test, root, settings.build_timeout)
            test_logs.append(f"### {rel} [test]{' TIMEOUT' if test_timed_out else ''}\n{test_log}")
            if not test_ok:
                test_failed = True

    build_output = "\n\n".join(build_logs) + (f"\n\n{note}" if note else "")
    test_output = "\n\n".join(test_logs)

    if build_failed:
        return {
            "status": "failed", "success": False, "output": build_output,
            "projects": [str(p) for _, p in projects.values()], "unsupported_files": unsupported,
            "test_status": None, "test_output": test_output,
        }

    if any_test_ran:
        test_status = "failed" if test_failed else "passed"
    else:
        test_status = None
        if not test_output:
            test_output = "No runnable test command for this project type (or no tests found)."

    return {
        "status": "passed", "success": True, "output": build_output,
        "projects": [str(p) for _, p in projects.values()], "unsupported_files": unsupported,
        "test_status": test_status, "test_output": test_output,
    }
