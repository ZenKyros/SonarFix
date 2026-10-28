"""Dependency vulnerability scanning: which packages a project depends on
have a known CVE, what version fixes it, and whether upgrading is a safe
(patch/minor) or risky (major) change.

Uses each ecosystem's own vulnerability data where one exists - `dotnet
list package --vulnerable` for NuGet, `npm audit` for npm - since those
already know exactly what is installed. Maven and Gradle have no such
built-in checker, so their direct dependencies are parsed from the
manifest and checked against OSV.dev (Google's free, no-auth Open Source
Vulnerabilities database) instead.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import httpx

from .sonar import _ssl_context

_SKIP_DIRS = {
    ".git", "node_modules", "bin", "obj", "target", "build", "dist",
    ".sonarfix-build", "venv", ".venv",
}

_OSV_BATCH_URL = "https://api.osv.dev/v1/querybatch"
_OSV_VULN_URL = "https://api.osv.dev/v1/vulns/{id}"


@dataclass
class DependencyFinding:
    ecosystem: str          # "NuGet" | "npm" | "Maven" | "Gradle"
    package: str
    current_version: str
    fixed_version: str | None
    severity: str           # "critical" | "high" | "moderate" | "low" | "unknown"
    advisory_id: str
    manifest_path: str
    direct: bool
    bump_kind: str          # "patch" | "minor" | "major" | "unknown"
    recommendation: str     # "upgrade" | "review"
    summary: str = ""       # one-line description of the vulnerability itself
    reason: str = ""        # plain-English explanation of the recommendation
    latest_version: str = ""  # newest stable release in the registry, if known

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _version_tuple(v: str) -> tuple[int, ...] | None:
    """'12.0.1-beta' -> (12, 0, 1). None if it doesn't start with digits."""
    cleaned = re.match(r"^\s*v?(\d+(?:\.\d+)*)", v or "")
    if not cleaned:
        return None
    return tuple(int(p) for p in cleaned.group(1).split("."))


def _bump_kind(current: str, fixed: str | None) -> str:
    cur, fix = _version_tuple(current), _version_tuple(fixed or "")
    if not cur or not fix:
        return "unknown"
    if fix[0] != cur[0]:
        return "major"
    if fix[1:2] != cur[1:2]:
        return "minor"
    return "patch"


def _recommendation(bump_kind: str) -> str:
    # A major bump can change behaviour even if it compiles - that needs a
    # human to actually read the changelog, not just a mechanical swap.
    return "review" if bump_kind == "major" else "upgrade"


_SEVERITY_PHRASE = {
    "critical": "a critical-severity vulnerability",
    "high": "a high-severity vulnerability",
    "moderate": "a moderate-severity vulnerability",
    "low": "a low-severity vulnerability",
    "unknown": "a vulnerability whose severity couldn't be automatically assessed",
}


def _reason(
    bump_kind: str, severity: str, direct: bool, current: str,
    fixed: str | None, summary: str, cvss_vector: str,
) -> str:
    """Plain-English answer to "why is this flagged, and is upgrading
    actually necessary" - not just the bare recommendation tag."""
    scope = "a direct dependency" if direct else "an indirect (transitive) dependency"
    sev_phrase = _SEVERITY_PHRASE.get(severity, "a vulnerability")
    what = f"{summary.rstrip('.')}. " if summary else ""
    vector_note = f" (CVSS vector: {cvss_vector})" if severity == "unknown" and cvss_vector else ""

    if not fixed:
        return (
            f"{what}This is {sev_phrase}{vector_note} in {scope}. No fixed version could be "
            "confirmed automatically - check the advisory directly before deciding whether to act."
        )
    if bump_kind == "major":
        return (
            f"{what}This is {sev_phrase}{vector_note} in {scope}. The fix ({current} → {fixed}) is a "
            "major version bump, which can include breaking API changes - worth doing, but read the "
            "changelog first rather than applying it mechanically."
        )
    return (
        f"{what}This is {sev_phrase}{vector_note} in {scope}. The fix ({current} → {fixed}) is a "
        f"{bump_kind} version bump, which by semantic-versioning convention shouldn't break anything - "
        "safe to apply directly."
    )


def _finding(
    ecosystem: str, package: str, current: str, fixed: str | None,
    severity: str, advisory_id: str, manifest_path: str, direct: bool,
    summary: str = "", cvss_vector: str = "",
) -> DependencyFinding:
    bump = _bump_kind(current, fixed)
    severity = (severity or "unknown").lower()
    return DependencyFinding(
        ecosystem=ecosystem, package=package, current_version=current,
        fixed_version=fixed, severity=severity,
        advisory_id=advisory_id, manifest_path=manifest_path, direct=direct,
        bump_kind=bump, recommendation=_recommendation(bump),
        summary=summary,
        reason=_reason(bump, severity, direct, current, fixed, summary, cvss_vector),
    )


def _walk_manifests(repo_root: Path, names: tuple[str, ...]) -> list[Path]:
    found: list[Path] = []
    for path in repo_root.rglob("*"):
        if not path.is_file() or path.name not in names:
            continue
        if any(part in _SKIP_DIRS for part in path.relative_to(repo_root).parts):
            continue
        found.append(path)
    return found


# --- .NET / NuGet -------------------------------------------------------

_DOTNET_VULN_LINE = re.compile(
    r"^\s*>\s+(?P<pkg>\S+)\s+(?P<rest>.+?)\s+"
    r"(?P<severity>Low|Moderate|High|Critical)\s+(?P<url>\S+)\s*$"
)
_GHSA_RE = re.compile(r"(GHSA-[\w-]+)", re.IGNORECASE)


def _normalize_ghsa(raw: str) -> str:
    """OSV.dev's vuln-detail endpoint is case-sensitive: 'GHSA-' uppercase,
    everything after it lowercase - `dotnet`/`npm` output doesn't guarantee
    that casing, so a lookup built from it as-is silently 404s."""
    return "GHSA-" + raw.split("-", 1)[1].lower() if "-" in raw else raw


_osv_detail_cache: dict[str, dict[str, Any]] = {}


def _osv_vuln_detail(vuln_id: str) -> dict[str, Any]:
    """OSV.dev's detail endpoint, cached per id - every ecosystem looks up
    the same advisory this way, so a package hit by the same CVE in two
    manifests only costs one network call."""
    if not vuln_id:
        return {}
    if vuln_id in _osv_detail_cache:
        return _osv_detail_cache[vuln_id]
    try:
        with httpx.Client(timeout=15.0, verify=_ssl_context()) as client:
            response = client.get(_OSV_VULN_URL.format(id=vuln_id))
        detail = response.json() if response.status_code == 200 else {}
    except (httpx.HTTPError, ValueError):
        detail = {}
    _osv_detail_cache[vuln_id] = detail
    return detail


def _osv_fixed_from_detail(detail: dict[str, Any], ecosystem: str, name: str) -> str | None:
    """The first version where every affected range is fixed, for the given
    package - a package name match is case-insensitive since NuGet/npm
    report their own casing, which doesn't always match OSV's."""
    for affected in detail.get("affected", []):
        pkg = affected.get("package", {})
        if pkg.get("ecosystem") != ecosystem or pkg.get("name", "").lower() != name.lower():
            continue
        for rng in affected.get("ranges", []):
            fixed_versions = [e["fixed"] for e in rng.get("events", []) if "fixed" in e]
            if fixed_versions:
                return fixed_versions[-1]
    return None


def _osv_summary(detail: dict[str, Any]) -> str:
    return (detail.get("summary") or "").strip()


def _osv_severity_and_vector(detail: dict[str, Any]) -> tuple[str, str]:
    """(severity label, raw CVSS vector). OSV's `severity[].score` is the
    full CVSS vector string, not a number, so it can't be turned into a
    label without implementing the CVSS base-score formula - the vector is
    surfaced as-is instead, so at least the detail is visible to whoever's
    deciding, even when a plain-English label isn't available."""
    vector = ""
    for sev in detail.get("severity", []):
        score = sev.get("score", "")
        if score.startswith("CVSS"):
            vector = score
            break
    database_specific = detail.get("database_specific", {})
    label = (database_specific.get("severity") or "unknown").lower()
    return label, vector


def _scan_dotnet(csproj_files: list[Path], repo_root: Path) -> list[DependencyFinding]:
    dotnet = shutil.which("dotnet")
    if not dotnet or not csproj_files:
        return []
    findings: list[DependencyFinding] = []
    seen: set[tuple[str, str, str]] = set()  # (project, package, severity) dedup across net TFMs
    for csproj in csproj_files:
        try:
            result = subprocess.run(
                [dotnet, "list", str(csproj), "package", "--vulnerable", "--include-transitive"],
                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120,
            )
        except (subprocess.TimeoutExpired, OSError):
            continue
        rel_manifest = str(csproj.relative_to(repo_root))
        is_transitive_section = False
        for line in result.stdout.splitlines():
            if "Transitive Package" in line:
                is_transitive_section = True
            elif "Top-level Package" in line:
                is_transitive_section = False
            match = _DOTNET_VULN_LINE.match(line)
            if not match:
                continue
            package = match.group("pkg")
            versions = match.group("rest").split()
            current = versions[-1] if versions else ""
            dedup_key = (rel_manifest, package, current)
            if dedup_key in seen:
                continue
            seen.add(dedup_key)
            ghsa = _GHSA_RE.search(match.group("url"))
            advisory_id = _normalize_ghsa(ghsa.group(1)) if ghsa else match.group("url")
            detail = _osv_vuln_detail(advisory_id) if ghsa else {}
            fixed = _osv_fixed_from_detail(detail, "NuGet", package) if detail else None
            findings.append(
                _finding(
                    "NuGet", package, current, fixed, match.group("severity"),
                    advisory_id, rel_manifest, not is_transitive_section,
                    summary=_osv_summary(detail),
                )
            )
    return findings


# --- npm ------------------------------------------------------------------


def _npm_installed_version(project_dir: Path, name: str) -> str:
    """The version actually in node_modules - npm audit only reports the
    vulnerable *range*, which can't be classified as a patch/minor/major bump."""
    try:
        data = json.loads((project_dir / "node_modules" / name / "package.json").read_text(encoding="utf-8"))
        return str(data.get("version") or "")
    except (OSError, ValueError):
        return ""


def _scan_npm(package_json_files: list[Path], repo_root: Path) -> list[DependencyFinding]:
    npm = shutil.which("npm") or shutil.which("npm.cmd")
    if not npm:
        return []
    findings: list[DependencyFinding] = []
    for package_json in package_json_files:
        project_dir = package_json.parent
        if not (project_dir / "node_modules").exists():
            continue  # npm audit needs an installed tree; an un-built repo has nothing to report
        try:
            result = subprocess.run(
                [npm, "audit", "--json"],
                cwd=str(project_dir), capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=120,
            )
        except (subprocess.TimeoutExpired, OSError):
            continue
        try:
            data = json.loads(result.stdout or "{}")
        except ValueError:
            continue
        rel_manifest = str(package_json.relative_to(repo_root))
        for name, vuln in (data.get("vulnerabilities") or {}).items():
            fix = vuln.get("fixAvailable")
            fixed_version = fix.get("version") if isinstance(fix, dict) else None
            via = vuln.get("via") or []
            advisory_id = ""
            npm_summary = ""
            for item in via:
                if isinstance(item, dict) and item.get("url"):
                    npm_summary = item.get("title") or ""
                    ghsa = _GHSA_RE.search(item["url"])
                    advisory_id = _normalize_ghsa(ghsa.group(1)) if ghsa else item["url"]
                    break
            findings.append(
                _finding(
                    "npm", name, _npm_installed_version(project_dir, name)
                    or vuln.get("range", "") or "installed", fixed_version,
                    vuln.get("severity", "unknown"), advisory_id, rel_manifest,
                    direct=bool(vuln.get("isDirect")),
                    summary=npm_summary,
                )
            )
    return findings


# --- Maven / Gradle, via OSV.dev -------------------------------------------

_MAVEN_DEP_RE = re.compile(
    r"<dependency>\s*<groupId>([^<]+)</groupId>\s*<artifactId>([^<]+)</artifactId>"
    r"\s*<version>([^<]+)</version>", re.MULTILINE,
)
_GRADLE_DEP_RE = re.compile(
    r"""(?:implementation|api|compile|runtimeOnly|testImplementation)\s*"""
    r"""[\(\s]*['"]([\w.-]+):([\w.-]+):([\w.\-+]+)['"]""",
)


def _osv_query_batch(packages: list[tuple[str, str, str]]) -> dict[tuple[str, str], list[str]]:
    """packages: (ecosystem, name, version) -> {(name, version): [vuln_id, ...]}."""
    if not packages:
        return {}
    queries = [
        {"package": {"name": name, "ecosystem": eco}, "version": version}
        for eco, name, version in packages
    ]
    try:
        with httpx.Client(timeout=30.0, verify=_ssl_context()) as client:
            response = client.post(_OSV_BATCH_URL, json={"queries": queries})
        response.raise_for_status()
        results = response.json().get("results", [])
    except (httpx.HTTPError, ValueError):
        return {}
    out: dict[tuple[str, str], list[str]] = {}
    for (_, name, version), result in zip(packages, results):
        vuln_ids = [v["id"] for v in result.get("vulns", [])]
        if vuln_ids:
            out[(name, version)] = vuln_ids
    return out


def _scan_manifest_ecosystem(
    manifests: list[Path], repo_root: Path, display_ecosystem: str, pattern: re.Pattern,
) -> list[DependencyFinding]:
    """`display_ecosystem` is what the UI shows ("Maven" or "Gradle"); the
    OSV.dev query always uses "Maven" since that's OSV's ecosystem name for
    anything resolved from Maven Central, regardless of which build tool."""
    osv_ecosystem = "Maven"
    # group, artifact, version per manifest, so a vuln can be traced back to it
    deps_by_manifest: dict[Path, list[tuple[str, str, str]]] = {}
    for manifest in manifests:
        try:
            text = manifest.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        deps = [
            (group, artifact, version)
            for group, artifact, version in pattern.findall(text)
            if "${" not in version  # a property placeholder we can't resolve without a full POM model
        ]
        if deps:
            deps_by_manifest[manifest] = deps

    all_packages = [
        (osv_ecosystem, f"{group}:{artifact}", version)
        for deps in deps_by_manifest.values()
        for group, artifact, version in deps
    ]
    vulns_by_pkg = _osv_query_batch(all_packages)
    if not vulns_by_pkg:
        return []

    findings: list[DependencyFinding] = []
    for manifest, deps in deps_by_manifest.items():
        rel_manifest = str(manifest.relative_to(repo_root))
        for group, artifact, version in deps:
            name = f"{group}:{artifact}"
            for vid in vulns_by_pkg.get((name, version), []):
                detail = _osv_vuln_detail(vid)
                severity, vector = _osv_severity_and_vector(detail)
                fixed = _osv_fixed_from_detail(detail, osv_ecosystem, name)
                findings.append(
                    _finding(
                        display_ecosystem, name, version, fixed, severity,
                        vid, rel_manifest, True,
                        summary=_osv_summary(detail), cvss_vector=vector,
                    )
                )
    return findings


# --- orchestration ----------------------------------------------------------


def scan_project(repo_path: str) -> list[dict[str, Any]]:
    """Every outdated-and-vulnerable dependency found across all ecosystems
    present in this repo. Best-effort per ecosystem: a missing tool (no
    `dotnet`/`npm` on PATH, no installed node_modules) just means that
    ecosystem contributes nothing, never an error for the whole scan."""
    root = Path(repo_path).resolve()
    findings: list[DependencyFinding] = []

    csproj_files = [
        p for p in root.rglob("*.csproj")
        if not any(part in _SKIP_DIRS for part in p.relative_to(root).parts)
    ]
    findings += _scan_dotnet(csproj_files, root)

    package_json_files = _walk_manifests(root, ("package.json",))
    findings += _scan_npm(package_json_files, root)

    pom_files = _walk_manifests(root, ("pom.xml",))
    findings += _scan_manifest_ecosystem(pom_files, root, "Maven", _MAVEN_DEP_RE)

    gradle_files = _walk_manifests(root, ("build.gradle", "build.gradle.kts"))
    findings += _scan_manifest_ecosystem(gradle_files, root, "Gradle", _GRADLE_DEP_RE)

    # Track what is merely outdated too, and note the latest release on every row.
    from . import deps_upgrade

    try:
        findings += deps_upgrade.enrich_and_find_outdated(
            root, findings, deps_upgrade.manifests_for(root)
        )
    except Exception:  # noqa: BLE001 - registry lookups are best-effort
        pass

    return [f.to_dict() for f in findings]
