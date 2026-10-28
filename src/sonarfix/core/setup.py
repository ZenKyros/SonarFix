"""First-run setup: store credentials and get the Claude Code CLI installed and signed in.

Everything lives in the user's .env (PROJECT_ROOT), so the same file works for the
packaged .exe and for a source checkout.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

from .config import PROJECT_ROOT, ConfigError, get_settings

ENV_PATH = PROJECT_ROOT / ".env"

# Fields the Settings page may write, mapped to their .env names.
FIELDS = {
    "sonar_url": "SONAR_URL",
    "sonar_token": "SONAR_TOKEN",
    "sonar_org": "SONAR_ORG",
    "bitbucket_username": "BITBUCKET_USERNAME",
    "bitbucket_token": "BITBUCKET_TOKEN",
}

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


# --- .env ---------------------------------------------------------------------


def write_env(values: dict[str, str]) -> None:
    """Upsert KEY=value lines, keeping comments and unrelated keys untouched."""
    lines = ENV_PATH.read_text(encoding="utf-8").splitlines() if ENV_PATH.exists() else []
    pending = dict(values)
    for i, line in enumerate(lines):
        if line.lstrip().startswith("#"):
            continue
        match = re.match(r"\s*([A-Z][A-Z0-9_]*)\s*=", line)
        if match and match.group(1) in pending:
            lines[i] = f"{match.group(1)}={pending.pop(match.group(1))}"
    lines += [f"{key}={value}" for key, value in pending.items()]
    ENV_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
    for key, value in values.items():
        os.environ[key] = value
    get_settings.cache_clear()


def save_credentials(body: dict[str, str | None]) -> None:
    """Blank fields keep their current value, so a token is never wiped by accident."""
    updates: dict[str, str] = {}
    for field, env_name in FIELDS.items():
        value = (body.get(field) or "").strip()
        if field == "sonar_url":
            value = value.rstrip("/")
        if value:
            updates[env_name] = value
    if updates:
        write_env(updates)


def credentials_status() -> dict[str, Any]:
    s = get_settings()
    return {
        "sonar_url": s.sonar_url,
        "sonar_org": s.sonar_org,
        "sonar_token_set": bool(s.sonar_token),
        "bitbucket_username": s.bitbucket_username,
        "bitbucket_token_set": bool(s.bitbucket_token),
    }


# --- Claude Code CLI ------------------------------------------------------------


def find_claude() -> str | None:
    found = shutil.which("claude")
    if found:
        return found
    home = Path.home()
    for candidate in (
        home / ".local" / "bin" / "claude.exe",
        home / ".local" / "bin" / "claude",
        home / ".claude" / "local" / "claude.exe",
        Path(os.environ.get("APPDATA", "")) / "npm" / "claude.cmd",
    ):
        if candidate.is_file():
            return str(candidate)
    return None


def _run(cmd: list[str], timeout: int = 20) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd, capture_output=True, text=True, timeout=timeout, creationflags=_NO_WINDOW
    )


def claude_status() -> dict[str, Any]:
    path = find_claude()
    if not path:
        return {"installed": False, "logged_in": False}
    info: dict[str, Any] = {"installed": True, "path": path, "logged_in": False}
    try:
        info["version"] = _run([path, "--version"]).stdout.strip()
        data = json.loads(_run([path, "auth", "status"]).stdout)
        info["logged_in"] = bool(data.get("loggedIn"))
        info["email"] = data.get("email")
        info["auth_method"] = data.get("authMethod")
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        info["error"] = str(exc)
    return info


def install_claude() -> dict[str, Any]:
    """Run Anthropic's official installer (per-user, no admin rights needed)."""
    if find_claude():
        return claude_status()
    steps = [
        [
            "powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command",
            "irm https://claude.ai/install.ps1 | iex",
        ]
    ]
    if shutil.which("npm"):
        steps.append(["npm", "install", "-g", "@anthropic-ai/claude-code"])
    errors = []
    for cmd in steps:
        try:
            result = _run(cmd, timeout=600)
        except (OSError, subprocess.SubprocessError) as exc:
            errors.append(str(exc))
            continue
        if find_claude():
            return claude_status()
        errors.append((result.stderr or result.stdout).strip()[-300:])
    raise ConfigError(
        "Could not install the Claude Code CLI automatically "
        f"({' | '.join(e for e in errors if e) or 'no output'}). "
        "Install it from https://claude.ai/install and click Refresh."
    )


def start_claude_login() -> None:
    """Open the CLI's browser sign-in (SSO) in its own console window."""
    path = find_claude()
    if not path:
        raise ConfigError("Install the Claude Code CLI first.")
    flags = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)
    subprocess.Popen([path, "auth", "login"], creationflags=flags)
