"""Bitbucket: clone, push a branch, open a pull request.

Supports Bitbucket Cloud (bitbucket.org) and Bitbucket Server / Data Center.
The token is handed to git through environment config (`http.extraHeader`), so
it never lands in a remote URL, in `.git/config`, or in process arguments.

    BITBUCKET_TOKEN     repository/project access token, or an app password
    BITBUCKET_USERNAME  set only for basic auth (app passwords); blank = Bearer
    BITBUCKET_URL       Server/DC base URL when it cannot be read from the remote
"""

from __future__ import annotations

import base64
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import httpx

from .config import get_settings
from .sonar import _ssl_context


class ScmError(RuntimeError):
    """Pushing or opening the pull request failed."""


@dataclass(frozen=True)
class BitbucketRepo:
    kind: str  # "cloud" | "server"
    api_base: str
    owner: str  # workspace (cloud) or project key (server)
    slug: str
    web_base: str

    @property
    def label(self) -> str:
        return f"{self.owner}/{self.slug}"


_SCP_RE = re.compile(r"^(?:[\w.-]+@)?([\w.-]+):(.+)$")


def parse_remote(url: str) -> BitbucketRepo | None:
    """Recognise a Bitbucket clone URL; None for GitHub, GitLab, etc."""
    url = url.strip()
    if "://" not in url:  # scp-like: git@bitbucket.org:ws/repo.git
        match = _SCP_RE.match(url)
        if not match:
            return None
        host, path, scheme = match.group(1), match.group(2), "ssh"
    else:
        parsed = urlparse(url)
        host, path, scheme = parsed.hostname or "", parsed.path, parsed.scheme

    parts = [p for p in path.strip("/").split("/") if p]
    if parts and parts[-1].endswith(".git"):
        parts[-1] = parts[-1][:-4]

    if host == "bitbucket.org" and len(parts) >= 2:
        return BitbucketRepo(
            kind="cloud",
            api_base="https://api.bitbucket.org/2.0",
            owner=parts[0],
            slug=parts[1],
            web_base="https://bitbucket.org",
        )

    settings = get_settings()
    configured = urlparse(settings.bitbucket_url).hostname if settings.bitbucket_url else None
    lowered = [p.lower() for p in parts]
    if "scm" in lowered and len(parts) >= lowered.index("scm") + 3:
        i = lowered.index("scm")
        context = "/".join(parts[:i])
        base = settings.bitbucket_url or (
            f"https://{host}" + (f"/{context}" if context else "")
        )
        return BitbucketRepo("server", base, parts[i + 1], parts[i + 2], base)
    if scheme == "ssh" and (configured == host or "bitbucket" in host) and len(parts) >= 2:
        base = settings.bitbucket_url or f"https://{host}"
        return BitbucketRepo("server", base, parts[-2], parts[-1], base)
    return None


def _auth_header() -> str:
    settings = get_settings()
    if not settings.bitbucket_token:
        raise ScmError(
            "BITBUCKET_TOKEN is not set. Add a repository access token (or an app "
            "password with BITBUCKET_USERNAME) to .env and restart the API."
        )
    if settings.bitbucket_username:
        pair = f"{settings.bitbucket_username}:{settings.bitbucket_token}".encode()
        return "Basic " + base64.b64encode(pair).decode()
    return f"Bearer {settings.bitbucket_token}"


def _git_env() -> dict[str, str]:
    env = dict(os.environ)
    env.update(
        {
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "http.extraHeader",
            "GIT_CONFIG_VALUE_0": f"Authorization: {_auth_header()}",
        }
    )
    return env


def _run_git(args: list[str], cwd: str | Path | None = None, timeout: int = 600) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        env=_git_env(),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )
    if result.returncode != 0:
        raise ScmError(f"git {args[0]} failed: {(result.stderr or result.stdout).strip()[:600]}")
    return result.stdout


def origin_url(repo_path: str | Path) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo_path), "remote", "get-url", "origin"],
        capture_output=True,
        text=True,
    )
    return result.stdout.strip() if result.returncode == 0 else ""


def status(repo_path: str | Path | None) -> dict[str, object]:
    """What the UI needs to decide whether 'Create PR' can work. No network."""
    settings = get_settings()
    remote = origin_url(repo_path) if repo_path else ""
    repo = parse_remote(remote) if remote else None
    if not remote:
        reason = "The local clone has no 'origin' remote."
    elif not repo:
        host = urlparse(remote).hostname if "://" in remote else remote.split(":")[0]
        reason = f"origin points at {host}; pull requests are supported for Bitbucket."
    elif not settings.bitbucket_token:
        reason = "Set BITBUCKET_TOKEN in .env to push and open pull requests."
    else:
        reason = None
    return {
        "ready": reason is None,
        "reason": reason,
        "provider": f"bitbucket-{repo.kind}" if repo else None,
        "repository": repo.label if repo else None,
    }


def clone(url: str, dest: Path, branch: str | None = None) -> None:
    if dest.exists() and any(dest.iterdir()):
        raise ScmError(f"{dest} already exists and is not empty.")
    dest.parent.mkdir(parents=True, exist_ok=True)
    args = ["clone", "--single-branch"]
    if branch:
        args += ["--branch", branch]
    _run_git([*args, url, str(dest)], timeout=1800)


def push(repo_path: str | Path, branch: str) -> None:
    _run_git(["push", "--set-upstream", "origin", branch], cwd=repo_path)


def open_pull_request(
    repo_path: str | Path, branch: str, base: str, title: str, description: str
) -> str:
    remote = origin_url(repo_path)
    repo = parse_remote(remote)
    if not repo:
        raise ScmError(f"origin ({remote or 'missing'}) is not a Bitbucket repository.")

    if repo.kind == "cloud":
        url = f"{repo.api_base}/repositories/{repo.owner}/{repo.slug}/pullrequests"
        body = {
            "title": title,
            "description": description,
            "source": {"branch": {"name": branch}},
            "destination": {"branch": {"name": base}},
            "close_source_branch": True,
        }
    else:
        url = f"{repo.api_base}/rest/api/1.0/projects/{repo.owner}/repos/{repo.slug}/pull-requests"
        body = {
            "title": title,
            "description": description,
            "fromRef": {"id": f"refs/heads/{branch}"},
            "toRef": {"id": f"refs/heads/{base}"},
        }

    with httpx.Client(verify=_ssl_context(), timeout=60.0) as client:
        response = client.post(
            url,
            json=body,
            headers={"Authorization": _auth_header(), "Accept": "application/json"},
        )
    if response.status_code >= 400:
        raise ScmError(
            f"Bitbucket refused the pull request ({response.status_code}): "
            f"{response.text[:500]}"
        )
    links = response.json().get("links", {})
    if repo.kind == "cloud":
        return links.get("html", {}).get("href", "")
    selfs = links.get("self") or [{}]
    return selfs[0].get("href", "")
