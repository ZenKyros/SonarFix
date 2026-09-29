"""Thin SonarQube / SonarCloud web-API client.

Only the three calls the MVP needs: projects, issues, rule description.
"""

from __future__ import annotations

import html
import re
import ssl
from functools import lru_cache
from typing import Any

import httpx

from .config import get_settings

_TAG_RE = re.compile(r"<[^>]+>")
_BLANK_RE = re.compile(r"\n{3,}")

# SonarQube refuses paginated requests past this offset.
_MAX_ISSUES = 10_000
_PAGE_SIZE = 500


class SonarError(RuntimeError):
    """A SonarQube call failed."""


@lru_cache(maxsize=1)
def _ssl_context() -> ssl.SSLContext | bool:
    """Trust whatever the operating system trusts.

    Corporate networks commonly terminate TLS with an internal CA that is in
    the Windows/macOS certificate store but not in certifi's bundle, which
    otherwise fails as CERTIFICATE_VERIFY_FAILED. `truststore` bridges the two.
    """
    try:
        import truststore
    except ImportError:
        return True
    return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)


def _client() -> httpx.Client:
    settings = get_settings()
    settings.require_sonar()
    # Sonar accepts the user token as the basic-auth username with an empty
    # password. This works on SonarCloud and on every SonarQube version.
    return httpx.Client(
        base_url=settings.sonar_url,
        auth=(settings.sonar_token, ""),
        verify=_ssl_context(),
        timeout=30.0,
        headers={"Accept": "application/json"},
    )


def _get(client: httpx.Client, path: str, **params: Any) -> dict[str, Any]:
    response = client.get(path, params={k: v for k, v in params.items() if v})
    if response.status_code >= 400:
        raise SonarError(
            f"GET {path} returned {response.status_code}: {response.text[:300]}"
        )
    return response.json()


def strip_html(value: str) -> str:
    """Sonar rule descriptions are HTML; the model only needs the prose."""
    text = html.unescape(_TAG_RE.sub("", value or ""))
    return _BLANK_RE.sub("\n\n", text).strip()


def list_projects() -> list[dict[str, str]]:
    """Every project visible to the token.

    `/api/projects/search` needs admin rights on some instances, so fall back
    to `/api/components/search`, which any authenticated user can call.
    """
    org = get_settings().sonar_org
    with _client() as client:
        try:
            payload = _get(
                client, "/api/projects/search", organization=org, ps=_PAGE_SIZE
            )
        except SonarError:
            payload = _get(
                client,
                "/api/components/search",
                organization=org,
                qualifiers="TRK",
                ps=_PAGE_SIZE,
            )
    return [
        {"key": component["key"], "name": component.get("name") or component["key"]}
        for component in payload.get("components", [])
    ]


def list_issues(project_key: str) -> list[dict[str, Any]]:
    """All unresolved issues for a project, following pagination."""
    issues: list[dict[str, Any]] = []
    org = get_settings().sonar_org
    with _client() as client:
        page = 1
        while True:
            payload = _get(
                client,
                "/api/issues/search",
                organization=org,
                componentKeys=project_key,
                resolved="false",
                ps=_PAGE_SIZE,
                p=page,
            )
            batch = payload.get("issues", [])
            issues.extend(batch)
            total = payload.get("paging", {}).get("total", len(issues))
            if (
                not batch
                or len(issues) >= min(total, _MAX_ISSUES)
                or page * _PAGE_SIZE >= _MAX_ISSUES
            ):
                break
            page += 1
    return issues


def get_rule(rule_key: str) -> dict[str, str]:
    """Rule name plus its description as plain text."""
    if not rule_key:
        return {"key": "", "name": "", "description": ""}
    with _client() as client:
        try:
            payload = _get(
                client, "/api/rules/show", organization=get_settings().sonar_org, key=rule_key
            )
        except SonarError:
            return {"key": rule_key, "name": rule_key, "description": ""}

    rule = payload.get("rule", {})
    description = rule.get("htmlDesc") or ""
    if not description:
        # SonarQube 9.6+ splits the description into sections.
        description = "\n\n".join(
            section.get("content", "")
            for section in rule.get("descriptionSections", [])
        )
    return {
        "key": rule.get("key", rule_key),
        "name": rule.get("name", rule_key),
        "description": strip_html(description),
    }


def list_organizations() -> list[dict[str, str]]:
    """Organizations the token belongs to. SonarCloud only; empty elsewhere."""
    with _client() as client:
        try:
            payload = _get(client, "/api/organizations/search", member="true", ps=100)
        except SonarError:
            return []
    return [
        {"key": org["key"], "name": org.get("name") or org["key"]}
        for org in payload.get("organizations", [])
    ]


def get_repo_binding(project_key: str) -> dict[str, str] | None:
    """The git remote SonarCloud/SonarQube has bound to this project for
    PR decoration, if any (GitHub/GitLab/Bitbucket/Azure DevOps ALM binding)."""
    with _client() as client:
        try:
            payload = _get(
                client, "/api/alm_settings/get_binding", project=project_key
            )
        except SonarError:
            return None
    alm = payload.get("alm")
    repo = payload.get("repository") or payload.get("slug")
    url = payload.get("url")
    if not (alm and (repo or url)):
        return None
    return {"alm": alm, "repository": repo or "", "url": url or ""}


def clone_url_for_binding(binding: dict[str, str]) -> str | None:
    """Best-effort https clone URL from an ALM binding."""
    if binding.get("url"):
        url = binding["url"]
        return url if url.endswith(".git") else url
    alm = binding.get("alm", "")
    repo = binding.get("repository", "")
    if alm == "github" and repo:
        return f"https://github.com/{repo}.git"
    if alm == "gitlab" and repo:
        return f"https://gitlab.com/{repo}.git"
    if alm == "bitbucket" and repo:
        return f"https://bitbucket.org/{repo}.git"
    return None
