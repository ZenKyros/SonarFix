"""Local build + test verification: does the fix compile, and do tests still pass,
before we propose a PR.

Finds the project (a .csproj/.vbproj for C#/.NET, a pom.xml for Maven, a
build.gradle(.kts) for Gradle, a CMakeLists.txt for CMake, or a .vcxproj for
C++/Visual Studio) owning each file the fix touched, and builds (then tests)
just those - the "blast radius". Also reports the build artifact each project
produces (a .dll/.exe/.jar/...), so a reviewer can see what's actually shipping
without reading every changed file.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

from .config import get_settings

# Project marker file -> project kind, checked nearest-first while walking up
# from a changed file. A directory should only ever have one kind of marker.
_DOTNET_GLOBS = ("*.csproj", "*.vbproj")
_CPP_GLOBS = ("*.vcxproj",)
_MAVEN_MARKER = "pom.xml"
_GRADLE_MARKERS = ("build.gradle", "build.gradle.kts")
_CMAKE_MARKER = "CMakeLists.txt"


def _project_for(repo_root: Path, file_path: str) -> tuple[str, Path] | None:
    """Nearest project file above `file_path`, as (kind, path), if any."""
    current = (repo_root / file_path).resolve().parent
    while True:
        dotnet_matches = []
        for pattern in _DOTNET_GLOBS:
            dotnet_matches.extend(sorted(current.glob(pattern)))
        if dotnet_matches:
            return "dotnet", dotnet_matches[0]
        cpp_matches = sorted(p for pattern in _CPP_GLOBS for p in current.glob(pattern))
        if cpp_matches:
            return "cpp-vs", cpp_matches[0]
        maven = current / _MAVEN_MARKER
        if maven.is_file():
            return "maven", maven
        for name in _GRADLE_MARKERS:
            gradle = current / name
            if gradle.is_file():
                return "gradle", gradle
        cmake = current / _CMAKE_MARKER
        if cmake.is_file():
            return "cmake", cmake
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
    """What to run to build and test one project, or None if the tool is missing.

    `build`/`test` are each a list of steps (a step is argv) run in order,
    stopping at the first failing step - CMake needs a configure step before
    its build step, everything else is a single step.
    """

    def __init__(
        self, build: list[list[str]] | None, test: list[list[str]] | None, note: str = ""
    ) -> None:
        self.build = build
        self.test = test
        self.note = note


def _dotnet_commands(csproj: Path) -> _Commands:
    dotnet = shutil.which("dotnet")
    msbuild = _find_msbuild()
    if _is_sdk_style(csproj) and dotnet:
        return _Commands(
            build=[[dotnet, "build", str(csproj), "--nologo", "-v", "minimal"]],
            test=[[dotnet, "test", str(csproj), "--nologo", "-v", "minimal"]],
        )
    if msbuild:
        return _Commands(build=[[msbuild, str(csproj), "/nologo", "/v:minimal"]], test=None)
    if dotnet:
        return _Commands(
            build=[[dotnet, "build", str(csproj), "--nologo", "-v", "minimal"]],
            test=[[dotnet, "test", str(csproj), "--nologo", "-v", "minimal"]],
        )
    return _Commands(build=None, test=None, note="Neither 'dotnet' nor MSBuild.exe is on PATH.")


def _cpp_vs_commands(vcxproj: Path) -> _Commands:
    msbuild = _find_msbuild()
    if not msbuild:
        return _Commands(build=None, test=None, note="MSBuild.exe is not on PATH.")
    # No standard test runner for a bare .vcxproj - CMake+ctest covers testing.
    return _Commands(build=[[msbuild, str(vcxproj), "/nologo", "/v:minimal"]], test=None)


def _cmake_commands(cmakelists: Path) -> _Commands:
    cmake = shutil.which("cmake")
    if not cmake:
        return _Commands(build=None, test=None, note="'cmake' is not on PATH.")
    source_dir = cmakelists.parent
    build_dir = source_dir / ".sonarfix-build"
    configure = [cmake, "-S", str(source_dir), "-B", str(build_dir)]
    build = [cmake, "--build", str(build_dir)]
    ctest = shutil.which("ctest")
    test = [[ctest, "--test-dir", str(build_dir), "--output-on-failure"]] if ctest else None
    return _Commands(build=[configure, build], test=test)


def _maven_commands(pom: Path, repo_root: Path) -> _Commands:
    tool = _wrapper(pom.parent, repo_root, ("mvnw", "mvnw.cmd")) or shutil.which("mvn")
    if not tool:
        return _Commands(build=None, test=None, note="Neither a Maven wrapper nor 'mvn' is on PATH.")
    return _Commands(
        build=[[tool, "-q", "-f", str(pom), "-DskipTests", "compile"]],
        test=[[tool, "-q", "-f", str(pom), "test"]],
    )


def _gradle_commands(marker: Path, repo_root: Path) -> _Commands:
    tool = _wrapper(marker.parent, repo_root, ("gradlew", "gradlew.bat")) or shutil.which("gradle")
    if not tool:
        return _Commands(build=None, test=None, note="Neither a Gradle wrapper nor 'gradle' is on PATH.")
    project_dir = marker.parent
    return _Commands(
        build=[[tool, "-q", "-p", str(project_dir), "build", "-x", "test"]],
        test=[[tool, "-q", "-p", str(project_dir), "test"]],
    )


def _commands_for(kind: str, marker: Path, repo_root: Path) -> _Commands:
    if kind == "dotnet":
        return _dotnet_commands(marker)
    if kind == "cpp-vs":
        return _cpp_vs_commands(marker)
    if kind == "cmake":
        return _cmake_commands(marker)
    if kind == "maven":
        return _maven_commands(marker, repo_root)
    return _gradle_commands(marker, repo_root)


# --- what ships: the artifact each project kind produces --------------------


def _dotnet_artifact(csproj: Path) -> dict[str, str]:
    try:
        text = csproj.read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        text = ""
    name_match = re.search(r"<AssemblyName>\s*([^<]+?)\s*</AssemblyName>", text)
    name = (name_match.group(1) if name_match else csproj.stem).strip()
    output_match = re.search(r"<OutputType>\s*([^<]+?)\s*</OutputType>", text)
    output_type = (output_match.group(1) if output_match else "Library").strip().lower()
    ext = ".exe" if output_type in ("exe", "winexe") else ".dll"
    return {"name": name, "file": f"{name}{ext}", "kind": "assembly"}


def _cpp_vs_artifact(vcxproj: Path) -> dict[str, str]:
    try:
        text = vcxproj.read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        text = ""
    config_match = re.search(r"<ConfigurationType>\s*([^<]+?)\s*</ConfigurationType>", text)
    config_type = (config_match.group(1) if config_match else "Application").strip()
    ext = {"DynamicLibrary": ".dll", "StaticLibrary": ".lib"}.get(config_type, ".exe")
    name = vcxproj.stem
    return {"name": name, "file": f"{name}{ext}", "kind": "native binary"}


def _cmake_artifact(cmakelists: Path) -> dict[str, str]:
    try:
        text = cmakelists.read_text(encoding="utf-8", errors="replace")
    except OSError:
        text = ""
    project_match = re.search(r"project\s*\(\s*([A-Za-z0-9_.-]+)", text)
    name = project_match.group(1) if project_match else cmakelists.parent.name
    if re.search(r"add_library\s*\(\s*" + re.escape(name) + r"\b[^)]*\bSHARED\b", text, re.IGNORECASE):
        return {"name": name, "file": f"{name}.dll / .so", "kind": "native binary"}
    if re.search(r"add_library\s*\(\s*" + re.escape(name), text, re.IGNORECASE):
        return {"name": name, "file": f"{name}.lib / .a", "kind": "native binary"}
    return {"name": name, "file": f"{name} (executable)", "kind": "native binary"}


def _maven_artifact(pom: Path) -> dict[str, str]:
    try:
        text = pom.read_text(encoding="utf-8", errors="replace")
    except OSError:
        text = ""
    artifact_match = re.search(r"<artifactId>\s*([^<]+?)\s*</artifactId>", text)
    name = artifact_match.group(1) if artifact_match else pom.parent.name
    packaging_match = re.search(r"<packaging>\s*([^<]+?)\s*</packaging>", text)
    packaging = (packaging_match.group(1) if packaging_match else "jar").strip()
    return {"name": name, "file": f"{name}.{packaging}", "kind": "java package"}


def _gradle_artifact(marker: Path) -> dict[str, str]:
    name = marker.parent.name
    return {"name": name, "file": f"{name}.jar", "kind": "java package"}


def _artifact_for(kind: str, marker: Path) -> dict[str, str]:
    artifact = {
        "dotnet": _dotnet_artifact,
        "cpp-vs": _cpp_vs_artifact,
        "cmake": _cmake_artifact,
        "maven": _maven_artifact,
        "gradle": _gradle_artifact,
    }[kind](marker)
    artifact["project"] = str(marker)
    return artifact


def _unsupported_note(unsupported: list[str]) -> str:
    return (
        "Build verification covers C#/.NET, C++ (CMake or Visual Studio), "
        "Maven and Gradle projects. "
        f"{len(unsupported)} changed file(s) are in another project type and "
        f"were not built: {', '.join(unsupported)}"
    )


def _run_steps(steps: list[list[str]], root: Path, timeout: int) -> tuple[bool, str, bool]:
    """Run each step in order; stop at the first failure. Returns (ok, log, timed_out)."""
    logs: list[str] = []
    for step in steps:
        start = time.monotonic()
        try:
            result = subprocess.run(
                step, cwd=str(root), capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            logs.append(f"$ {' '.join(step)}\nTimed out after {timeout}s.")
            return False, "\n\n".join(logs), True

        duration = time.monotonic() - start
        tail = (result.stdout or "")[-4000:]
        if result.stderr:
            tail += "\n" + result.stderr[-2000:]
        logs.append(f"$ {' '.join(step)} ({duration:.1f}s)\n{tail.strip()}")
        if result.returncode != 0:
            return False, "\n\n".join(logs), False
    return True, "\n\n".join(logs), False


def format_artifacts(artifacts: list[dict[str, Any]]) -> str:
    """Markdown section listing what actually ships, for the PR description -
    the other team's reviewer needs this to know what to re-test, without
    reading every changed file."""
    if not artifacts:
        return ""
    lines = ["### Affected build artifacts", ""]
    for artifact in artifacts:
        project = Path(artifact.get("project", "")).name
        lines.append(
            f"- **{artifact.get('file')}** ({artifact.get('kind')}) — from `{project}`"
        )
        for changed in artifact.get("files") or []:
            lines.append(f"  - `{changed}`")
    return "\n".join(lines)


def verify(repo_path: str, changed_files: list[str]) -> dict[str, Any]:
    """Build, then run tests for, every project the changed files belong to.

    Never blocks the fix from being committed - purely informational at this
    stage (the gate on opening a PR lives in the caller). Files not owned by
    a recognised project file are reported back as `unsupported_files`
    instead of being silently ignored.

    Returns {"status": "passed"|"failed"|"skipped", "success": bool,
    "output": str, "projects": [str], "unsupported_files": [str],
    "test_status": "passed"|"failed"|"skipped"|None, "test_output": str,
    "artifacts": [{"project": str, "name": str, "file": str, "kind": str, "files": [str]}]}
    - shaped so it can be stored and shown to the user directly. `artifacts`
    is what a reviewer needs to know what's actually shipping (a .dll, .exe,
    .jar, ...) without reading every changed file.
    """
    settings = get_settings()
    empty = {
        "status": "skipped", "success": True, "output": "",
        "projects": [], "unsupported_files": [],
        "test_status": None, "test_output": "", "artifacts": [],
    }
    if not settings.build_enabled:
        empty["output"] = "Build verification is disabled (SONARFIX_BUILD_ENABLED=false)."
        return empty

    root = Path(repo_path).resolve()
    projects: dict[str, tuple[str, Path]] = {}
    project_files: dict[str, list[str]] = {}
    unsupported: list[str] = []
    for file_path in changed_files:
        found = _project_for(root, file_path)
        if found:
            kind, marker = found
            key = str(marker)
            projects[key] = (kind, marker)
            project_files.setdefault(key, []).append(file_path)
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
    artifacts: list[dict[str, str]] = []
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

        ok, log, timed_out = _run_steps(commands.build, root, settings.build_timeout)
        build_logs.append(f"### {rel} [build]{' TIMEOUT' if timed_out else ''}\n{log}")
        if not ok:
            build_failed = True
            continue  # don't test code that doesn't even compile

        artifact = _artifact_for(kind, marker)
        artifact["files"] = project_files.get(str(marker), [])
        artifacts.append(artifact)

        if commands.test:
            any_test_ran = True
            test_ok, test_log, test_timed_out = _run_steps(commands.test, root, settings.build_timeout)
            test_logs.append(f"### {rel} [test]{' TIMEOUT' if test_timed_out else ''}\n{test_log}")
            if not test_ok:
                test_failed = True

    build_output = "\n\n".join(build_logs) + (f"\n\n{note}" if note else "")
    test_output = "\n\n".join(test_logs)

    if build_failed:
        return {
            "status": "failed", "success": False, "output": build_output,
            "projects": [str(p) for _, p in projects.values()], "unsupported_files": unsupported,
            "test_status": None, "test_output": test_output, "artifacts": artifacts,
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
        "test_status": test_status, "test_output": test_output, "artifacts": artifacts,
    }
