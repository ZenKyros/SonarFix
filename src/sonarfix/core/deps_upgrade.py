"""Upgrade tracking and impact analysis for a project's dependencies.

`deps.py` answers "which packages have a known CVE". This module answers the
follow-up a reviewer actually has: *should I upgrade it, is it safe, and what
will change if I do?*

Two parts:

* `outdated_findings` - every direct dependency that is behind its registry's
  latest release (npm registry, NuGet flat container, Maven Central), so the
  tracker lists what is merely stale as well as what is vulnerable.
* `analyze_upgrade` - a trial upgrade. The repo is copied to a scratch folder,
  the dependency is bumped there (never in the real working tree), and the
  owning project is rebuilt and retested with the same build verifier used for
  fixes. The result is a verdict (safe / caution / risky), the manifest diff,
  the packages that move with it, compiler errors if it breaks, and the source
  files that actually reference the package.
"""

from __future__ import annotations

import difflib
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

from . import build
from .config import get_settings
from .deps import (
    _GRADLE_DEP_RE, _MAVEN_DEP_RE, _SKIP_DIRS, DependencyFinding, _bump_kind,
    _version_tuple, _walk_manifests,
)
from .sonar import _ssl_context

_COPY_CAP_BYTES = 400 * 1024 * 1024
_LOCKFILES = ("package-lock.json", "packages.lock.json")
_NON_REGISTRY_PREFIXES = ("file:", "link:", "git", "http", "workspace:", "npm:", "github:", "portal:")

_latest_cache: dict[tuple[str, str], dict[str, str]] = {}


# --- direct dependencies of one manifest -------------------------------------


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        return ""


def _npm_direct(manifest: Path) -> list[tuple[str, str]]:
    try:
        data = json.loads(_read(manifest) or "{}")
    except ValueError:
        return []
    out: list[tuple[str, str]] = []
    declared = {**(data.get("devDependencies") or {}), **(data.get("dependencies") or {})}
    for name, spec in declared.items():
        spec = str(spec).strip()
        if spec.startswith(_NON_REGISTRY_PREFIXES):
            continue
        installed = ""
        installed_json = manifest.parent / "node_modules" / name / "package.json"
        if installed_json.is_file():
            try:
                installed = str(json.loads(_read(installed_json)).get("version") or "")
            except ValueError:
                installed = ""
        version = installed or (re.search(r"\d+(?:\.\d+)*(?:-[\w.]+)?", spec) or [""])[0]
        if version:
            out.append((name, version))
    return out


def _nuget_direct(manifest: Path) -> list[tuple[str, str]]:
    text = _read(manifest)
    out: list[tuple[str, str]] = []
    if manifest.name.lower() == "packages.config":
        for m in re.finditer(r'<package\s[^>]*?\bid="([^"]+)"[^>]*?\bversion="([^"]+)"', text, re.I):
            out.append((m.group(1), m.group(2)))
        return out
    for m in re.finditer(r"<PackageReference\b([^>]*?)(/?)>", text):
        name = re.search(r'Include\s*=\s*"([^"]+)"', m.group(1))
        if not name:
            continue
        ver = re.search(r'Version\s*=\s*"([^"]+)"', m.group(1))
        version = ver.group(1) if ver else ""
        if not version and not m.group(2):
            end = text.find("</PackageReference>", m.end())
            inner = text[m.end(): end if end != -1 else m.end() + 300]
            child = re.search(r"<Version>\s*([^<]+?)\s*</Version>", inner)
            version = child.group(1) if child else ""
        if version and not re.search(r"[$\[\(*,]", version):
            out.append((name.group(1), version))
    return out


def _jvm_direct(manifest: Path, pattern: re.Pattern) -> list[tuple[str, str]]:
    return [
        (f"{g}:{a}", v) for g, a, v in pattern.findall(_read(manifest)) if "${" not in v
    ]


def direct_dependencies(manifest: Path, ecosystem: str) -> list[tuple[str, str]]:
    if ecosystem == "npm":
        return _npm_direct(manifest)
    if ecosystem == "NuGet":
        return _nuget_direct(manifest)
    if ecosystem == "Maven":
        return _jvm_direct(manifest, _MAVEN_DEP_RE)
    return _jvm_direct(manifest, _GRADLE_DEP_RE)


# --- latest release lookup ---------------------------------------------------


def _client() -> httpx.Client:
    return httpx.Client(timeout=15.0, verify=_ssl_context(), follow_redirects=True)


def _max_stable(versions: list[str]) -> str:
    stable = [v for v in versions if "-" not in v and _version_tuple(v)]
    return max(stable, key=lambda v: _version_tuple(v) or (), default="")


def _lookup_latest(ecosystem: str, name: str) -> dict[str, str]:
    """{"version", "changelog"} for the newest stable release, {} if unknown."""
    key = (ecosystem, name)
    if key in _latest_cache:
        return _latest_cache[key]
    info: dict[str, str] = {}
    try:
        with _client() as client:
            if ecosystem == "npm":
                r = client.get(f"https://registry.npmjs.org/{quote(name, safe='@')}/latest")
                if r.status_code == 200:
                    data = r.json()
                    repo = data.get("repository")
                    repo_url = repo.get("url", "") if isinstance(repo, dict) else str(repo or "")
                    repo_url = re.sub(r"^git\+|\.git$", "", repo_url).replace("git://", "https://")
                    info = {
                        "version": data.get("version", ""),
                        "changelog": f"{repo_url}/releases" if repo_url.startswith("http")
                        else f"https://www.npmjs.com/package/{name}?activeTab=versions",
                    }
            elif ecosystem == "NuGet":
                r = client.get(f"https://api.nuget.org/v3-flatcontainer/{name.lower()}/index.json")
                if r.status_code == 200:
                    version = _max_stable(r.json().get("versions", []))
                    info = {
                        "version": version,
                        "changelog": f"https://www.nuget.org/packages/{name}/{version}#releasenotes-body-tab",
                    }
            else:  # Maven / Gradle both resolve from Maven Central
                group, _, artifact = name.partition(":")
                base = f"https://repo1.maven.org/maven2/{group.replace('.', '/')}/{artifact}"
                r = client.get(f"{base}/maven-metadata.xml")
                if r.status_code == 200:
                    root = ET.fromstring(r.text)
                    versions = [v.text or "" for v in root.iter("version")]
                    info = {
                        "version": _max_stable(versions),
                        "changelog": f"https://mvnrepository.com/artifact/{group}/{artifact}",
                    }
    except (httpx.HTTPError, ValueError, ET.ParseError):
        info = {}
    _latest_cache[key] = info
    return info


def _outdated_reason(pkg: str, current: str, latest: str, bump: str, direct: bool = True) -> str:
    gap = {
        "major": "a major version behind - the newer release can contain breaking API changes",
        "minor": "a minor version behind - new features, normally backwards compatible",
        "patch": "a patch version behind - bug fixes only, normally backwards compatible",
    }.get(bump, "behind the latest release")
    return (
        f"No known vulnerability. {pkg} {current} is {gap} (latest stable: {latest}). "
        "Upgrading is optional housekeeping; run \"Analyze upgrade\" to see whether it builds cleanly."
    )


def enrich_and_find_outdated(
    root: Path, findings: list[DependencyFinding],
    manifests: dict[str, list[Path]],
) -> list[DependencyFinding]:
    """Stamp `latest_version` on the vulnerable findings and return a new
    finding for every other direct dependency that has a newer release.

    `manifests` maps ecosystem -> manifest files (csproj / package.json /
    pom.xml / build.gradle). Lookups run in parallel and are cached per
    package, so a dependency shared by many projects costs one request."""
    pairs: list[tuple[str, Path, str, str]] = []  # ecosystem, manifest, name, version
    for ecosystem, files in manifests.items():
        for manifest in files:
            for name, version in direct_dependencies(manifest, ecosystem):
                pairs.append((ecosystem, manifest, name, version))
    unique = {(eco, name) for eco, _, name, _ in pairs} | {(f.ecosystem, f.package) for f in findings}
    lookup_eco = lambda eco: "Maven" if eco == "Gradle" else eco  # noqa: E731
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda k: _lookup_latest(lookup_eco(k[0]), k[1]), unique))

    for f in findings:
        info = _latest_cache.get((lookup_eco(f.ecosystem), f.package)) or {}
        f.latest_version = info.get("version", "")

    vulnerable = {(f.ecosystem, f.manifest_path, f.package) for f in findings}
    out: list[DependencyFinding] = []
    for ecosystem, manifest, name, version in pairs:
        rel = str(manifest.relative_to(root))
        if (ecosystem, rel, name) in vulnerable:
            continue
        latest = (_latest_cache.get((lookup_eco(ecosystem), name)) or {}).get("version", "")
        cur_t, lat_t = _version_tuple(version), _version_tuple(latest)
        if not cur_t or not lat_t or lat_t <= cur_t:
            continue
        bump = _bump_kind(version, latest)
        out.append(DependencyFinding(
            ecosystem=ecosystem, package=name, current_version=version,
            fixed_version=None, severity="none", advisory_id="", manifest_path=rel,
            direct=True, bump_kind=bump,
            recommendation="review" if bump == "major" else "upgrade",
            summary=f"Newer release available: {latest}",
            reason=_outdated_reason(name, version, latest, bump),
            latest_version=latest,
        ))
    return out


# --- usage in source ---------------------------------------------------------

_SRC_EXTS = {
    "npm": (".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".vue", ".html"),
    "NuGet": (".cs", ".vb", ".fs", ".razor", ".cshtml"),
    "Maven": (".java", ".kt", ".scala", ".groovy"),
    "Gradle": (".java", ".kt", ".scala", ".groovy"),
}


def _usage_pattern(ecosystem: str, package: str) -> re.Pattern:
    if ecosystem == "npm":
        return re.compile(
            r"""(?:\bfrom\s+|\brequire\(\s*|\bimport\(\s*|\bimport\s+)['"]"""
            + re.escape(package) + r"""(?:/[^'"]*)?['"]"""
        )
    if ecosystem == "NuGet":
        return re.compile(
            r"^\s*(?:global\s+)?(?:using|Imports)\s+(?:static\s+)?" + re.escape(package) + r"\b",
            re.MULTILINE,
        )
    # A Maven group rarely equals the Java package (jackson-databind lives in
    # com.fasterxml.jackson.core but imports com.fasterxml.jackson.databind),
    # so also accept the group's parent - approximate, but errs towards "used".
    segments = package.split(":")[0].split(".")
    prefixes = {".".join(segments)}
    if len(segments) >= 3:
        prefixes.add(".".join(segments[:-1]))
    alternatives = "|".join(re.escape(p) for p in sorted(prefixes, key=len, reverse=True))
    return re.compile(r"^\s*import\s+(?:static\s+)?(?:" + alternatives + r")\b", re.MULTILINE)


def find_usage(root: Path, manifest_path: str, ecosystem: str, package: str) -> dict[str, Any]:
    """Source files under the manifest's folder that import the package -
    the code that would have to cope with any API change."""
    base = (root / manifest_path).parent
    pattern = _usage_pattern(ecosystem, package)
    exts = _SRC_EXTS.get(ecosystem, ())
    files: list[str] = []
    count = 0
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
        for fname in filenames:
            if not fname.lower().endswith(exts):
                continue
            path = Path(dirpath) / fname
            try:
                if path.stat().st_size > 1_000_000:
                    continue
                text = path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            if pattern.search(text):
                count += 1
                if len(files) < 15:
                    files.append(str(path.relative_to(root)).replace("\\", "/"))
    return {"count": count, "files": files}


# --- trial upgrade -----------------------------------------------------------


def _copy_ignore(_dir: str, names: list[str]) -> set[str]:
    return {n for n in names if n in _SKIP_DIRS or n == ".sonarfix-build"}


def _tree_size(path: Path, cap: int) -> int:
    total = 0
    for dirpath, dirnames, filenames in os.walk(path):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
        for fname in filenames:
            try:
                total += (Path(dirpath) / fname).stat().st_size
            except OSError:
                pass
            if total > cap:
                return total
    return total


def _stage_copy(root: Path, manifest_rel: str, ecosystem: str, dest: Path) -> Path:
    """Copy what the trial needs; returns the copy's root. Whole-repo for
    project-reference ecosystems, just the manifest folder if the repo is huge
    and the ecosystem is self-contained (npm)."""
    if _tree_size(root, _COPY_CAP_BYTES) <= _COPY_CAP_BYTES:
        shutil.copytree(root, dest, ignore=_copy_ignore)
        return dest
    if ecosystem == "npm":
        folder = (root / manifest_rel).parent
        rel_folder = folder.relative_to(root)
        shutil.copytree(folder, dest / rel_folder, ignore=_copy_ignore)
        return dest
    raise OSError("repository is too large to copy for a trial upgrade")


def _run(step: list[str], cwd: Path, timeout: int) -> tuple[bool, str]:
    try:
        result = subprocess.run(
            step, cwd=str(cwd), capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=timeout, env={**os.environ, "CI": "true"},
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        return False, f"{type(exc).__name__}: {exc}"
    return result.returncode == 0, ((result.stdout or "") + (result.stderr or "")).strip()


def _lock_versions(lock: Path) -> dict[str, str]:
    try:
        data = json.loads(_read(lock) or "{}")
    except ValueError:
        return {}
    out: dict[str, str] = {}
    for key, meta in (data.get("packages") or {}).items():
        if key and isinstance(meta, dict) and meta.get("version"):
            out[key.rsplit("node_modules/", 1)[-1]] = meta["version"]
    return out


def _changed_packages(before: dict[str, str], after: dict[str, str]) -> list[str]:
    lines: list[str] = []
    for name in sorted(set(before) | set(after)):
        old, new = before.get(name), after.get(name)
        if old == new:
            continue
        if old and new:
            lines.append(f"{name}: {old} → {new}")
        elif new:
            lines.append(f"{name}: added {new}")
        else:
            lines.append(f"{name}: removed (was {old})")
    return lines


def _apply_upgrade(
    copy_root: Path, manifest_rel: str, ecosystem: str, package: str,
    target: str, direct: bool, timeout: int,
) -> tuple[bool, str]:
    """Bump `package` to `target` inside the scratch copy. (ok, message)."""
    manifest = copy_root / manifest_rel
    folder = manifest.parent
    if ecosystem == "npm":
        npm = shutil.which("npm") or shutil.which("npm.cmd")
        if not npm:
            return False, "'npm' is not on PATH."
        if direct:
            step = [npm, "install", f"{package}@{target}", "--no-audit", "--no-fund"]
        else:
            # A transitive package can't be upgraded directly; pin it via overrides.
            try:
                data = json.loads(_read(manifest))
            except ValueError:
                return False, "package.json could not be parsed."
            data.setdefault("overrides", {})[package] = target
            manifest.write_text(json.dumps(data, indent=2), encoding="utf-8")
            step = [npm, "install", "--no-audit", "--no-fund"]
        return _run(step, folder, timeout)
    if ecosystem == "NuGet":
        dotnet = shutil.which("dotnet")
        if not dotnet:
            return False, "'dotnet' is not on PATH."
        if manifest.name.lower() == "packages.config":
            text = _read(manifest)
            new = re.sub(
                r'(<package\s[^>]*?\bid="%s"[^>]*?\bversion=")[^"]+(")' % re.escape(package),
                r"\g<1>%s\g<2>" % target, text, flags=re.I,
            )
            manifest.write_text(new, encoding="utf-8")
            return new != text, "packages.config edited"
        return _run([dotnet, "add", str(manifest), "package", package, "--version", target], folder, timeout)
    # Maven / Gradle: edit the version in the manifest text.
    text = _read(manifest)
    group, _, artifact = package.partition(":")
    if ecosystem == "Maven":
        pattern = re.compile(
            r"(<groupId>\s*%s\s*</groupId>\s*<artifactId>\s*%s\s*</artifactId>\s*<version>)[^<]+(</version>)"
            % (re.escape(group), re.escape(artifact))
        )
        new = pattern.sub(r"\g<1>%s\g<2>" % target, text)
    else:
        new = re.sub(
            r"(['\"]%s:%s:)[^'\"]+(['\"])" % (re.escape(group), re.escape(artifact)),
            r"\g<1>%s\g<2>" % target, text,
        )
    if new == text:
        return False, f"Could not find {package} in {manifest.name}."
    manifest.write_text(new, encoding="utf-8")
    return True, f"{manifest.name} edited"


def _error_lines(*outputs: str, limit: int = 8) -> list[str]:
    seen: list[str] = []
    for out in outputs:
        for line in (out or "").splitlines():
            if re.search(r"\berror\b|\bERR!|\bFAILED\b", line, re.I):
                clean = line.strip()[:300]
                if clean and clean not in seen:
                    seen.append(clean)
            if len(seen) >= limit:
                return seen
    return seen


def _verdict(
    finding: dict[str, Any], target: str, trial: dict[str, Any], usage: dict[str, Any],
) -> tuple[str, str, list[str]]:
    """(verdict, recommended action, reasons)."""
    bump = _bump_kind(finding["current_version"], target)
    reasons: list[str] = []
    verdict: str
    status = trial.get("status")
    if status == "ran":
        build_status, test_status = trial["build_status"], trial["test_status"]
        if build_status == "failed" and trial.get("baseline_build") == "failed":
            verdict = "unknown"
            reasons.append(
                "The project does not build even before the upgrade, so the impact of this "
                "upgrade could not be measured. Fix the existing build first."
            )
        elif build_status == "failed":
            verdict = "risky"
            reasons.append(
                "The project builds today but fails to build after this upgrade - the new "
                "version contains breaking changes your code has not been adapted to."
            )
        elif test_status == "failed":
            verdict = "risky"
            reasons.append(
                "The project builds after the upgrade but its tests fail - behaviour changed."
            )
        else:
            tests_ok = test_status == "passed"
            if bump == "major" and not tests_ok:
                verdict = "caution"
                reasons.append(
                    "Compiles after a major version bump, but no automated tests ran to confirm "
                    "runtime behaviour is unchanged - read the changelog and smoke-test."
                )
            else:
                verdict = "safe"
                reasons.append(
                    f"Builds cleanly after the upgrade{' and tests pass' if tests_ok else ''}."
                )
                if bump == "major":
                    reasons.append("It is a major bump, but the build and tests did not detect breakage.")
        if trial.get("note"):
            reasons.append(trial["note"])
    else:
        # Couldn't try it here (missing tool, huge repo): fall back to semver.
        why = trial.get("note") or "A trial build was not possible."
        if bump in ("patch", "minor"):
            verdict = "safe"
            reasons.append(f"{why} Judged only by semantic versioning: a {bump} bump should not break anything.")
        elif bump == "major":
            verdict = "caution"
            reasons.append(f"{why} It is a major version bump, which can include breaking changes.")
        else:
            verdict = "unknown"
            reasons.append(f"{why} The version change could not be classified.")

    if usage["count"] == 0 and finding.get("direct", True):
        reasons.append("No source file imports this package directly - small blast radius.")
    elif usage["count"]:
        reasons.append(f"{usage['count']} source file(s) import this package and would be affected by API changes.")

    severity = finding.get("severity", "none")
    if severity in ("critical", "high") and verdict in ("risky", "caution", "unknown"):
        reasons.append(
            f"The vulnerability is {severity}-severity, so do not leave it unpatched - "
            "plan the migration, or mitigate (e.g. restrict exposure) until you can."
        )
    action = {
        "safe": "Upgrade now",
        "caution": "Upgrade after review",
        "risky": "Do not upgrade yet - plan a migration",
        "unknown": "Investigate first",
    }[verdict]
    return verdict, action, reasons


def analyze_upgrade(
    repo_path: str, finding: dict[str, Any], target_version: str | None = None,
) -> dict[str, Any]:
    """Trial the upgrade of one dependency finding and say whether it is safe.

    Never touches the real working tree. Returns a JSON-able dict: verdict,
    action, reasons, target_version, bump_kind, changelog, usage, manifest_diff,
    changed_packages, build/test status and error excerpts, duration."""
    started = time.monotonic()
    root = Path(repo_path).resolve()
    ecosystem, package = finding["ecosystem"], finding["package"]
    manifest_rel = finding["manifest_path"]
    target = target_version or finding.get("fixed_version") or finding.get("latest_version") or ""
    lookup = _lookup_latest("Maven" if ecosystem == "Gradle" else ecosystem, package)
    result: dict[str, Any] = {
        "target_version": target,
        "bump_kind": _bump_kind(finding["current_version"], target) if target else "unknown",
        "changelog": lookup.get("changelog", ""),
        "usage": find_usage(root, manifest_rel, ecosystem, package),
        "manifest_diff": "", "changed_packages": [],
        "build_status": None, "test_status": None, "errors": [],
        "trial": {"status": "skipped", "note": ""},
    }
    if not target:
        result["trial"]["note"] = "No target version is known for this package."
        result.update(verdict="unknown", action="Investigate first",
                      reasons=[result["trial"]["note"]])
        result["duration_s"] = round(time.monotonic() - started, 1)
        return result

    timeout = get_settings().build_timeout
    scratch = Path(tempfile.mkdtemp(prefix="sonarfix-trial-"))
    try:
        try:
            copy_root = _stage_copy(root, manifest_rel, ecosystem, scratch / "repo")
        except OSError as exc:
            result["trial"]["note"] = f"Trial skipped: {exc}."
            copy_root = None
        if copy_root is not None:
            manifest = copy_root / manifest_rel
            before_text = _read(manifest)
            lock = manifest.parent / "package-lock.json"
            lock_before = _lock_versions(lock)

            ok, message = _apply_upgrade(
                copy_root, manifest_rel, ecosystem, package, target,
                bool(finding.get("direct", True)), timeout,
            )
            after_text = _read(manifest)
            result["manifest_diff"] = "".join(difflib.unified_diff(
                before_text.splitlines(keepends=True), after_text.splitlines(keepends=True),
                f"a/{manifest_rel}", f"b/{manifest_rel}", n=1,
            ))
            result["changed_packages"] = _changed_packages(lock_before, _lock_versions(lock))
            if not ok:
                # A resolver/restore failure *is* the impact: the new version doesn't fit.
                result["trial"] = {
                    "status": "ran",
                    "note": f"The upgrade could not even be applied: {message[-300:]}",
                }
                result["build_status"] = "failed"
                result["errors"] = _error_lines(message) or [message[-300:]]
            else:
                outcome = build.verify(str(copy_root), [manifest_rel])
                result["trial"] = {"status": "ran", "note": ""}
                missing_tool = re.search(r"on PATH", outcome.get("output", ""))
                if outcome["status"] == "skipped" or missing_tool:
                    # No toolchain here is a limit of this machine, not evidence about the upgrade.
                    note = (outcome.get("output", "") or "Build skipped.").strip().splitlines()[-1]
                    result["trial"] = {"status": "skipped", "note": f"Trial build not possible: {note}"}
                else:
                    result["build_status"] = outcome["status"]
                    result["test_status"] = outcome.get("test_status")
                    result["errors"] = _error_lines(outcome.get("output", ""), outcome.get("test_output", ""))
                    result["build_output_tail"] = (outcome.get("output") or "")[-1500:]
                    result["test_output_tail"] = (outcome.get("test_output") or "")[-1500:]

            # Did it already fail before we touched it?
            if result["trial"]["status"] == "ran" and result["build_status"] == "failed":
                manifest.write_text(before_text, encoding="utf-8")
                shutil.rmtree(manifest.parent / "node_modules", ignore_errors=True)
                for name in _LOCKFILES:
                    original = root / Path(manifest_rel).parent / name
                    if original.is_file():
                        shutil.copy2(original, manifest.parent / name)
                base = build.verify(str(copy_root), [manifest_rel])
                result["trial"]["baseline_build"] = base["status"]
    except Exception as exc:  # noqa: BLE001 - analysis is advisory, never fatal
        result["trial"] = {"status": "skipped", "note": f"Trial failed unexpectedly: {exc}"}
    finally:
        shutil.rmtree(scratch, ignore_errors=True)

    flat = {"status": result["trial"]["status"], "note": result["trial"].get("note", ""),
            "build_status": result["build_status"], "test_status": result["test_status"],
            "baseline_build": result["trial"].get("baseline_build")}
    verdict, action, reasons = _verdict(finding, target, flat, result["usage"])
    result.update(verdict=verdict, action=action, reasons=reasons)
    result["duration_s"] = round(time.monotonic() - started, 1)
    return result


def manifests_for(root: Path) -> dict[str, list[Path]]:
    """Every manifest the tracker understands, grouped by ecosystem."""
    csproj = [
        p for p in root.rglob("*.csproj")
        if not any(part in _SKIP_DIRS for part in p.relative_to(root).parts)
    ]
    return {
        "NuGet": csproj + _walk_manifests(root, ("packages.config",)),
        "npm": _walk_manifests(root, ("package.json",)),
        "Maven": _walk_manifests(root, ("pom.xml",)),
        "Gradle": _walk_manifests(root, ("build.gradle", "build.gradle.kts")),
    }
