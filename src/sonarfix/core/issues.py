"""Issues for the single-issue screen - always live from SonarQube.

The local DB never caches "what issues currently exist": that's SonarQube's
job, and a local copy of it goes stale the moment someone resolves an issue
there. What the DB does remember is what SonarFix did about an issue -
fix_plans/fix_runs snapshot the rule/file/line/message at analysis time (see
store.create_plan), so that history reads back correctly even though it was
never written here.

(The batch/plan screen and the PowerShell agentcli bridge are a separate,
older code path that still works off a synced local snapshot - they cluster
issues against actual file content and need a stable set to plan against.
This module is only for the "one issue at a time" flow: list, detail,
analyze, fix.)
"""

from __future__ import annotations

from typing import Any

from . import sonar, store

SEVERITY_RANK = store.SEVERITY_RANK

# A handful of repos (e.g. inf-src) are scanned in Sonar as several separate
# project keys that share one git repo and one branch. Each such group is
# presented as a single "composite" project so the one-repo-per-project fix/PR
# flow still applies; its issues are the union of the real Sonar projects.
COMPOSITE_PROJECTS: dict[str, dict[str, Any]] = {
    "INF-SRC:11.10-Support": {
        "name": "INF-SRC (11.10-Support)",
        "branch": "11.10-Support-infsrc",
        # inf-src is a monorepo: each Sonar sub-project lives in a folder
        # named after the part of its key after this prefix, e.g.
        # 'com.unisys.InfoImage:INF-SRC.DataInterfaceService' -> 'DataInterfaceService/'.
        "subfolder_prefix": "com.unisys.InfoImage:INF-SRC.",
        "component_keys": (
            "com.unisys.InfoImage:INF-SRC.IIFContentDownloader",
            "com.unisys.InfoImage:INF-SRC.ImageQualityAppService",
            "com.unisys.InfoImage:INF-SRC.IIFAzureADGateWay",
            "com.unisys.InfoImage:INF-SRC.IIFContentIngestionService",
            "com.unisys.InfoImage:INF-SRC.IIFMessageEncryption",
            "com.unisys.InfoImage:INF-SRC.IIFBulkDelete",
            "com.unisys.InfoImage:INF-SRC.DataInterfaceService",
        ),
    },
}


def _composite_owning(sonar_project: str) -> tuple[str, dict[str, Any]] | None:
    """(composite_key, config) of the composite project this real Sonar
    sub-project belongs to, if any."""
    for key, cfg in COMPOSITE_PROJECTS.items():
        if sonar_project in cfg["component_keys"]:
            return key, cfg
    return None


def _normalize(raw: dict[str, Any]) -> dict[str, Any]:
    """One Sonar API issue -> the flat shape the UI/workflow expect.

    `raw` is kept under "raw" too to carry textRange and anything else a
    recipe might need, the same data the old raw_json column held.
    """
    sonar_project = raw.get("project") or ""
    file_path = store.strip_component_prefix(raw.get("component") or "", sonar_project)
    project_key = sonar_project

    owning = _composite_owning(sonar_project)
    if owning:
        composite_key, cfg = owning
        prefix = cfg.get("subfolder_prefix") or ""
        if prefix and sonar_project.startswith(prefix):
            file_path = f"{sonar_project[len(prefix):]}/{file_path}"
        project_key = composite_key

    return {
        "id": raw.get("key"),
        "project_key": project_key,
        "rule": raw.get("rule"),
        "severity": raw.get("severity"),
        "type": raw.get("type"),
        "status": raw.get("status"),
        "message": raw.get("message"),
        "component": raw.get("component"),
        "file_path": file_path,
        "line": raw.get("line"),
        "raw": raw,
    }


def fetch_many(project_key: str) -> list[dict[str, Any]]:
    """Every open issue for `project_key`, fetched live and normalized."""
    composite = COMPOSITE_PROJECTS.get(project_key)
    if composite:
        raws = sonar.list_issues(",".join(composite["component_keys"]), branch=composite["branch"])
    else:
        raws = sonar.list_issues(project_key)
    return [_normalize(r) for r in raws]


def fetch_one(issue_id: str) -> dict[str, Any] | None:
    """One issue by its Sonar key, fetched live and normalized."""
    raw = sonar.get_issue(issue_id)
    return _normalize(raw) if raw else None


def filter_and_sort(
    items: list[dict[str, Any]], severity: str | None = None, issue_type: str | None = None
) -> list[dict[str, Any]]:
    if severity:
        items = [i for i in items if i["severity"] == severity]
    if issue_type:
        items = [i for i in items if i["type"] == issue_type]
    return sorted(items, key=lambda i: (SEVERITY_RANK.get(i["severity"], 9), i["file_path"] or ""))


def facets(project_key: str) -> dict[str, list[str]]:
    """Distinct severities and types present, for the UI filters."""
    items = fetch_many(project_key)
    severities = sorted({i["severity"] for i in items if i["severity"]}, key=lambda s: SEVERITY_RANK.get(s, 9))
    types = sorted({i["type"] for i in items if i["type"]})
    return {"severities": severities, "types": types}
