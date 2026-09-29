#!/usr/bin/env python
"""Auto-clone and set up repositories for all or selected SonarCloud projects.

    python scripts/setup_repo.py [--all | --project KEY [--project KEY ...]]

With --all: discovers and clones every project bound to your SonarCloud org.
With --project KEY: clones only the named projects (repeatable).
Without either: lists all available projects interactively.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from sonarfix.core import service, store
from sonarfix.core.config import ConfigError


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Auto-clone repos for SonarCloud projects."
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Clone every project in the org (those with repo bindings).",
    )
    parser.add_argument(
        "--project",
        action="append",
        dest="projects",
        help="Clone only these project keys (repeatable).",
    )
    parser.add_argument(
        "--workdir",
        type=Path,
        default=None,
        help="Directory to clone repos into (default: data/repos).",
    )
    args = parser.parse_args()

    # Initialize database if needed
    store.init_db()

    # Sync the project list from SonarCloud
    print("Discovering projects from SonarCloud...")
    service.sync_projects()
    all_projects = service.list_projects()
    print(f"[OK] Found {len(all_projects)} projects")
    print()

    # Determine which projects to set up
    if args.all:
        target_projects = all_projects
    elif args.projects:
        target_projects = [p for p in all_projects if p["key"] in args.projects]
        if len(target_projects) != len(args.projects):
            missing = set(args.projects) - {p["key"] for p in target_projects}
            print(f"Warning: projects not found: {', '.join(missing)}")
            print()
    else:
        print("Available projects:")
        for p in all_projects:
            print(f"  {p['key']:40} {p['name']}")
        print()
        print("Run with --all to clone all projects, or --project KEY [--project KEY ...]")
        print("to clone specific projects.")
        return 0

    if not target_projects:
        print("No projects to set up.")
        return 0

    print(f"Setting up {len(target_projects)} project(s)...")
    print()

    succeeded = 0
    failed = []

    for project in target_projects:
        key = project["key"]
        name = project["name"]
        print(f"{key:40} ({name})")

        try:
            # Clone/update the repo
            repo_path = service.ensure_repo(key, workdir=args.workdir)
            print(f"  [OK] Cloned to {repo_path}")

            # Sync issues
            result = service.sync_issues(key)
            issues_synced = result.get("issues_synced", 0)
            print(f"  [OK] Synced {issues_synced} issues")

            # Summary
            issues = store.list_issues(key)
            by_severity = {}
            for issue in issues:
                severity = issue.get("severity", "UNKNOWN")
                by_severity.setdefault(severity, []).append(issue)

            counts = " ".join(
                f"{severity}:{len(by_severity.get(severity, []))}"
                for severity in ["BLOCKER", "CRITICAL", "MAJOR", "MINOR", "INFO"]
                if by_severity.get(severity)
            )
            print(f"  [OK] {counts if counts else '(no issues)'}")
            succeeded += 1

        except ConfigError as exc:
            print(f"  [SKIP] {exc}")
            failed.append((key, str(exc)))
        except Exception as exc:  # noqa: BLE001
            print(f"  [FAIL] {exc}")
            failed.append((key, str(exc)))

        print()

    print(f"Setup complete: {succeeded}/{len(target_projects)} projects ready")
    if failed:
        print(f"Skipped/failed ({len(failed)}):")
        for key, reason in failed:
            print(f"  {key:40} {reason}")
    print()
    print("You can now:")
    print("  1. Run: uv run sonarfix api")
    print("  2. Run: uv run sonarfix ui")
    print("  3. Pick a project and analyze issues")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
