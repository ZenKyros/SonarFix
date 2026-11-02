"""Bitbucket and Azure DevOps: clone, push a branch, open a pull request.

Supports Bitbucket Cloud (bitbucket.org), Bitbucket Server / Data Center and
Azure DevOps Repos (dev.azure.com, *.visualstudio.com, Azure DevOps Server).
The provider is read from the local clone's `origin` remote. The token is handed to git through environment config (`http.extraHeader`), so
it never lands in a remote URL, in `.git/config`, or in process arguments.

    BITBUCKET_TOKEN     repository/project access token, or an app password
    BITBUCKET_USERNAME  set only for basic auth (app passwords); blank = Bearer
    BITBUCKET_URL       Server/DC base URL when it cannot be read from the remote
    AZURE_DEVOPS_TOKEN  Azure DevOps personal access token (Code: Read & write)
"""

from __future__ import annotations

import base64
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote, unquote, urlparse

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


@dataclass(frozen=True)
class AzureRepo:
    api_base: str  # collection root, e.g. https://dev.azure.com/<org>
    project: str
    slug: str

    @property
    def label(self) -> str:
        return f"{self.project}/{self.slug}"

    @property
    def web_url(self) -> str:
        return f"{self.api_base}/{quote(self.project, safe='')}/_git/{quote(self.slug, safe='')}"


_SCP_RE = re.compile(r"^(?:[\w.-]+@)?([\w.-]+):(.+)$")


def normalize_clone_url(url: str) -> str:
    """Turn a URL copied from a browser into one `git clone` can actually use.

    People paste the page they were looking at, not the clone URL: a
    Bitbucket Server project page (`.../projects/KEY/repos/slug/browse`) or a
    Bitbucket Cloud repo page (`bitbucket.org/workspace/repo/src/...`). Both
    render fine in a browser but are not git transport endpoints, so `git
    clone` against them fails as an auth error, not a 404 - confusing. An
    already-valid clone URL (scp-like, `/scm/...`, or ending in `.git`) is
    returned unchanged.
    """
    raw = url.strip()
    if not raw or "://" not in raw:
        return raw  # empty, or scp-like (git@host:owner/repo.git)

    parsed = urlparse(raw)
    parts = [p for p in parsed.path.strip("/").split("/") if p]
    lowered = [p.lower() for p in parts]

    if raw.rstrip("/").endswith(".git") or "scm" in lowered:
        return raw

    # Azure DevOps repo page: .../_git/repo[?path=...&version=...] -> .../_git/repo
    if "_git" in lowered and lowered.index("_git") + 1 < len(parts):
        end = lowered.index("_git") + 2
        netloc = parsed.netloc.rsplit("@", 1)[-1]
        return f"{parsed.scheme}://{netloc}/{'/'.join(parts[:end])}"

    # Bitbucket Server/DC project page: .../projects/KEY/repos/slug[/browse|/commits|...]
    if "projects" in lowered and "repos" in lowered:
        pi, ri = lowered.index("projects"), lowered.index("repos")
        if pi + 1 < len(parts) and ri + 1 < len(parts):
            project_key, slug = parts[pi + 1], parts[ri + 1]
            context = "/".join(parts[:pi])
            prefix = f"/{context}" if context else ""
            return f"{parsed.scheme}://{parsed.netloc}{prefix}/scm/{project_key}/{slug}.git"

    # Bitbucket Cloud repo page: bitbucket.org/workspace/repo[/src/...]
    if parsed.hostname == "bitbucket.org" and len(parts) >= 2:
        return f"{parsed.scheme}://{parsed.netloc}/{parts[0]}/{parts[1]}.git"

    return raw


def _split_remote(url: str) -> tuple[str, list[str], str]:
    """(host, path parts without a trailing .git, scheme) for https and scp-like remotes."""
    url = url.strip()
    if "://" not in url:
        match = _SCP_RE.match(url)
        if not match:
            return "", [], ""
        host, path, scheme = match.group(1), match.group(2), "ssh"
    else:
        parsed = urlparse(url)
        host, path, scheme = parsed.hostname or "", parsed.path, parsed.scheme
    parts = [unquote(p) for p in path.strip("/").split("/") if p]
    if parts and parts[-1].endswith(".git"):
        parts[-1] = parts[-1][:-4]
    return host, parts, scheme


def parse_azure_remote(url: str) -> AzureRepo | None:
    """Recognise an Azure DevOps Repos remote; None for anything else.

    https://dev.azure.com/<org>/<project>/_git/<repo>
    https://<org>.visualstudio.com/[DefaultCollection/]<project>/_git/<repo>
    https://<server>/tfs/<collection>/<project>/_git/<repo>   (Azure DevOps Server)
    git@ssh.dev.azure.com:v3/<org>/<project>/<repo>
    """
    host, parts, _ = _split_remote(url)
    if not host or not parts:
        return None
    lowered = [p.lower() for p in parts]
    host = host.lower()

    if host in ("ssh.dev.azure.com", "vs-ssh.visualstudio.com"):
        if len(parts) >= 4 and lowered[0] == "v3":
            return AzureRepo(f"https://dev.azure.com/{parts[1]}", parts[2], parts[3])
        return None

    if "_git" not in lowered or lowered.index("_git") + 1 >= len(parts):
        return None
    i = lowered.index("_git")
    if i == 0:
        return None
    repo = parts[i + 1]
    project = parts[i - 1]
    prefix = parts[: i - 1]
    if host == "dev.azure.com" and i == 1:
        # https://dev.azure.com/<org>/_git/<repo>: the project is named like the repo.
        project, prefix = repo, [parts[0]]
    base = f"https://{host}" + "".join("/" + quote(p, safe="") for p in prefix)
    return AzureRepo(base, project, repo)


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


def _azure_auth_header() -> str:
    settings = get_settings()
    if not settings.azure_devops_token:
        raise ScmError(
            "AZURE_DEVOPS_TOKEN is not set. Add a personal access token with "
            "Code (Read & write) scope on the Settings page or in .env."
        )
    pair = f":{settings.azure_devops_token}".encode()
    return "Basic " + base64.b64encode(pair).decode()


def _auth_header(remote: str = "") -> str:
    if remote and parse_azure_remote(remote):
        return _azure_auth_header()
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


def _git_env(remote: str = "") -> dict[str, str]:
    env = dict(os.environ)
    env.update(
        {
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "http.extraHeader",
            "GIT_CONFIG_VALUE_0": f"Authorization: {_auth_header(remote)}",
        }
    )
    return env


from . import repo as _repo  # noqa: E402,F401 - importing it puts git on PATH


def _run_git(
    args: list[str],
    cwd: str | Path | None = None,
    timeout: int = 600,
    remote: str = "",
) -> str:
    # Pushes run inside the clone, so its origin decides which token applies.
    remote = remote or (origin_url(cwd) if cwd else "")
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        env=_git_env(remote),
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


def has_token_for(remote: str) -> bool:
    """True when we hold the credential for the provider `remote` points at."""
    settings = get_settings()
    if parse_azure_remote(remote):
        return bool(settings.azure_devops_token)
    return bool(settings.bitbucket_token)


def status(repo_path: str | Path | None) -> dict[str, object]:
    """What the UI needs to decide whether 'Create PR' can work. No network."""
    settings = get_settings()
    remote = origin_url(repo_path) if repo_path else ""
    azure = parse_azure_remote(remote) if remote else None
    repo = None if azure else (parse_remote(remote) if remote else None)
    if not remote:
        reason = "The local clone has no 'origin' remote."
    elif azure:
        reason = (
            None
            if settings.azure_devops_token
            else "Set AZURE_DEVOPS_TOKEN (Settings page or .env) to push and open pull requests."
        )
    elif not repo:
        host = urlparse(remote).hostname if "://" in remote else remote.split(":")[0]
        reason = (
            f"origin points at {host}; pull requests are supported for "
            "Bitbucket and Azure DevOps."
        )
    elif not settings.bitbucket_token:
        reason = "Set BITBUCKET_TOKEN in .env to push and open pull requests."
    else:
        reason = None
    provider = "azure-devops" if azure else (f"bitbucket-{repo.kind}" if repo else None)
    return {
        "ready": reason is None,
        "reason": reason,
        "provider": provider,
        "repository": azure.label if azure else (repo.label if repo else None),
        "username": settings.bitbucket_username or _repo.branch_prefix(),
    }


def clone(url: str, dest: Path, branch: str | None = None) -> None:
    if dest.exists() and any(dest.iterdir()):
        raise ScmError(f"{dest} already exists and is not empty.")
    dest.parent.mkdir(parents=True, exist_ok=True)
    args = ["clone", "--single-branch"]
    if branch:
        args += ["--branch", branch]
    _run_git([*args, url, str(dest)], timeout=1800, remote=url)


def push(repo_path: str | Path, branch: str) -> None:
    _run_git(["push", "--set-upstream", "origin", branch], cwd=repo_path)


def open_pull_request(
    repo_path: str | Path, branch: str, base: str, title: str, description: str
) -> str:
    remote = origin_url(repo_path)
    azure = parse_azure_remote(remote)
    if azure:
        return _open_azure_pull_request(azure, branch, base, title, description)
    repo = parse_remote(remote)
    if not repo:
        raise ScmError(
            f"origin ({remote or 'missing'}) is not a Bitbucket or Azure DevOps repository."
        )

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


# Azure DevOps rejects longer values outright.
_AZURE_TITLE_MAX = 400
_AZURE_DESCRIPTION_MAX = 4000


def _open_azure_pull_request(
    repo: AzureRepo, branch: str, base: str, title: str, description: str
) -> str:
    url = (
        f"{repo.api_base}/{quote(repo.project, safe='')}/_apis/git/repositories/"
        f"{quote(repo.slug, safe='')}/pullrequests"
    )
    body = {
        "sourceRefName": f"refs/heads/{branch}",
        "targetRefName": f"refs/heads/{base}",
        "title": title[:_AZURE_TITLE_MAX],
        "description": description[:_AZURE_DESCRIPTION_MAX],
    }
    with httpx.Client(verify=_ssl_context(), timeout=60.0) as client:
        response = client.post(
            url,
            params={"api-version": "7.1"},
            json=body,
            headers={"Authorization": _azure_auth_header(), "Accept": "application/json"},
        )
    if response.status_code >= 400:
        raise ScmError(
            f"Azure DevOps refused the pull request ({response.status_code}): "
            f"{response.text[:500]}"
        )
    return f"{repo.web_url}/pullrequest/{response.json().get('pullRequestId', '')}"
