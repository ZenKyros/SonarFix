"""Local build verification: does the fix actually compile before we propose a PR.

Finds the .csproj/.vbproj owning each file the fix touched and builds just
those (the "blast radius"), with `dotnet build` if available, else MSBuild.
"""

from __future__ import annotations

import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

from .config import get_settings


def _csproj_for(repo_root: Path, file_path: str) -> Path | None:
    """Nearest .csproj/.vbproj above `file_path`, if any."""
    current = (repo_root / file_path).resolve().parent
    while True:
        matches = sorted(current.glob("*.csproj")) + sorted(current.glob("*.vbproj"))
        if matches:
            return matches[0]
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


def verify(repo_path: str, changed_files: list[str]) -> dict[str, Any]:
    """Build every project the changed files belong to.

    Returns {"status": "passed"|"failed"|"skipped", "success": bool,
    "output": str, "projects": [str]} - shaped so it can be stored and shown
    to the user directly.
    """
    settings = get_settings()
    if not settings.build_enabled:
        return {
            "status": "skipped", "success": True,
            "output": "Build verification is disabled (SONARFIX_BUILD_ENABLED=false).",
            "projects": [],
        }

    root = Path(repo_path).resolve()
    projects: dict[str, Path] = {}
    for file_path in changed_files:
        csproj = _csproj_for(root, file_path)
        if csproj:
            projects[str(csproj)] = csproj

    if not projects:
        return {
            "status": "skipped", "success": True,
            "output": "No .csproj/.vbproj owns the changed files; nothing to build.",
            "projects": [],
        }

    dotnet = shutil.which("dotnet")
    msbuild = _find_msbuild()
    if not dotnet and not msbuild:
        return {
            "status": "skipped", "success": True,
            "output": "Neither 'dotnet' nor MSBuild.exe is on PATH; skipping build verification.",
            "projects": [str(p) for p in projects.values()],
        }

    logs: list[str] = []
    for csproj in projects.values():
        rel = csproj.relative_to(root)
        if _is_sdk_style(csproj) and dotnet:
            tool, args = dotnet, ["build", str(csproj), "--nologo", "-v", "minimal"]
        elif msbuild:
            tool, args = msbuild, [str(csproj), "/nologo", "/v:minimal"]
        elif dotnet:
            tool, args = dotnet, ["build", str(csproj), "--nologo", "-v", "minimal"]
        else:
            logs.append(f"### {rel}\nLegacy project format needs MSBuild, which is not installed.")
            return {
                "status": "failed", "success": False, "output": "\n\n".join(logs),
                "projects": [str(p) for p in projects.values()],
            }

        start = time.monotonic()
        try:
            result = subprocess.run(
                [tool, *args], cwd=str(root), capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=settings.build_timeout,
            )
        except subprocess.TimeoutExpired:
            logs.append(f"### {rel}\nTimed out after {settings.build_timeout}s.")
            return {
                "status": "failed", "success": False, "output": "\n\n".join(logs),
                "projects": [str(p) for p in projects.values()],
            }

        duration = time.monotonic() - start
        tail = (result.stdout or "")[-4000:]
        if result.stderr:
            tail += "\n" + result.stderr[-2000:]
        logs.append(f"### {rel} ({duration:.1f}s)\n{tail.strip()}")

        if result.returncode != 0:
            return {
                "status": "failed", "success": False, "output": "\n\n".join(logs),
                "projects": [str(p) for p in projects.values()],
            }

    return {
        "status": "passed", "success": True, "output": "\n\n".join(logs),
        "projects": [str(p) for p in projects.values()],
    }
