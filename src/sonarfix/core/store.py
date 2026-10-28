"""SQLite persistence. Four tables, stdlib sqlite3, no ORM."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any, Iterable

from .config import get_settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    key             TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    repo_path       TEXT,
    default_branch  TEXT,
    last_synced_at  TEXT
);

CREATE TABLE IF NOT EXISTS issues (
    id           TEXT PRIMARY KEY,
    project_key  TEXT NOT NULL REFERENCES projects(key),
    rule         TEXT,
    severity     TEXT,
    type         TEXT,
    status       TEXT,
    message      TEXT,
    component    TEXT,
    file_path    TEXT,
    line         INTEGER,
    raw_json     TEXT,
    fetched_at   TEXT
);

CREATE INDEX IF NOT EXISTS idx_issues_project ON issues(project_key);

-- issue_id is NOT a foreign key: fix_plans snapshots the issue it was made
-- for (rule/severity/file/line/message) at analysis time, so a plan's
-- history never depends on that issue still being cached anywhere locally.
CREATE TABLE IF NOT EXISTS fix_plans (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    issue_id      TEXT NOT NULL,
    project_key   TEXT,
    rule          TEXT,
    severity      TEXT,
    type          TEXT,
    message       TEXT,
    file_path     TEXT,
    line          INTEGER,
    explanation   TEXT,
    root_cause    TEXT,
    impact        TEXT,
    plan_json     TEXT,
    testing_notes TEXT,
    confidence    REAL,
    status        TEXT NOT NULL DEFAULT 'pending',
    feedback      TEXT,
    model         TEXT,
    created_at    TEXT
);

CREATE INDEX IF NOT EXISTS idx_plans_issue ON fix_plans(issue_id);

CREATE TABLE IF NOT EXISTS fix_runs (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_id             INTEGER NOT NULL REFERENCES fix_plans(id),
    branch              TEXT,
    base_branch         TEXT,
    diff                TEXT,
    changes_summary     TEXT,
    commit_message      TEXT,
    pr_title            TEXT,
    pr_description      TEXT,
    pr_url              TEXT,
    testing_suggestions TEXT,
    build_status        TEXT,
    build_output        TEXT,
    test_status         TEXT,
    test_output         TEXT,
    artifacts           TEXT,
    status              TEXT,
    error               TEXT,
    created_at          TEXT
);

CREATE INDEX IF NOT EXISTS idx_runs_plan ON fix_runs(plan_id);

CREATE TABLE IF NOT EXISTS batches (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    project_key   TEXT NOT NULL REFERENCES projects(key),
    status        TEXT NOT NULL,
    selection     TEXT,
    steps         TEXT,
    base_branch   TEXT,
    branch        TEXT,
    diff          TEXT,
    pr_title      TEXT,
    pr_description TEXT,
    pr_url        TEXT,
    build_status  TEXT,
    build_output  TEXT,
    test_status   TEXT,
    test_output   TEXT,
    artifacts     TEXT,
    retry_of      INTEGER,
    error         TEXT,
    created_at    TEXT,
    updated_at    TEXT
);

CREATE INDEX IF NOT EXISTS idx_batches_project ON batches(project_key);

CREATE TABLE IF NOT EXISTS dependencies (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    project_key     TEXT NOT NULL REFERENCES projects(key),
    ecosystem       TEXT,
    package         TEXT,
    current_version TEXT,
    fixed_version   TEXT,
    severity        TEXT,
    advisory_id     TEXT,
    manifest_path   TEXT,
    is_direct       INTEGER,
    bump_kind       TEXT,
    recommendation  TEXT,
    summary         TEXT,
    reason          TEXT,
    scanned_at      TEXT
);

CREATE INDEX IF NOT EXISTS idx_deps_project ON dependencies(project_key);

-- Upgrade analyses outlive rescans (a rescan replaces `dependencies` rows), so
-- they are keyed by what was analysed, not by a dependency row id.
CREATE TABLE IF NOT EXISTS dependency_analyses (
    project_key     TEXT NOT NULL,
    ecosystem       TEXT NOT NULL,
    package         TEXT NOT NULL,
    manifest_path   TEXT NOT NULL,
    current_version TEXT NOT NULL,
    result          TEXT,
    analyzed_at     TEXT,
    PRIMARY KEY (project_key, ecosystem, package, manifest_path, current_version)
);
"""

# Severity order used for sorting in the UI (worst first).
SEVERITY_RANK = {
    "BLOCKER": 0,
    "CRITICAL": 1,
    "MAJOR": 2,
    "MINOR": 3,
    "INFO": 4,
}

# Dependency findings use the vulnerability database's own severity words
# (OSV.dev, npm audit, NuGet advisories), not Sonar's - kept separate so a
# dependency's "critical" doesn't silently collide with Sonar's.
DEP_SEVERITY_RANK = {
    "critical": 0,
    "high": 1,
    "moderate": 2,
    "low": 3,
    "unknown": 4,
    "none": 5,  # not vulnerable, just behind the latest release
}


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect() -> sqlite3.Connection:
    settings = get_settings()
    settings.db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(settings.db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


_PROJECTS_MIGRATIONS = (
    "ALTER TABLE projects ADD COLUMN default_branch TEXT",
)

_FIX_RUNS_MIGRATIONS = (
    "ALTER TABLE fix_runs ADD COLUMN base_branch TEXT",
    "ALTER TABLE fix_runs ADD COLUMN pr_title TEXT",
    "ALTER TABLE fix_runs ADD COLUMN pr_url TEXT",
    "ALTER TABLE fix_runs ADD COLUMN build_status TEXT",
    "ALTER TABLE fix_runs ADD COLUMN build_output TEXT",
    "ALTER TABLE fix_runs ADD COLUMN test_status TEXT",
    "ALTER TABLE fix_runs ADD COLUMN test_output TEXT",
    "ALTER TABLE fix_runs ADD COLUMN artifacts TEXT",
)

_BATCHES_MIGRATIONS = (
    "ALTER TABLE batches ADD COLUMN build_status TEXT",
    "ALTER TABLE batches ADD COLUMN build_output TEXT",
    "ALTER TABLE batches ADD COLUMN test_status TEXT",
    "ALTER TABLE batches ADD COLUMN test_output TEXT",
    "ALTER TABLE batches ADD COLUMN artifacts TEXT",
    "ALTER TABLE batches ADD COLUMN retry_of INTEGER",
)

_DEPENDENCIES_MIGRATIONS = (
    "ALTER TABLE dependencies ADD COLUMN summary TEXT",
    "ALTER TABLE dependencies ADD COLUMN reason TEXT",
    "ALTER TABLE dependencies ADD COLUMN latest_version TEXT",
)


def _migrate_fix_plans(conn: sqlite3.Connection) -> None:
    """Drop the issue_id foreign key and add snapshot columns, preserving rows.

    SQLite can't ALTER a foreign key away, so an older fix_plans table (one
    without `project_key`) is rebuilt under a temporary name, populated with a
    best-effort backfill from `issues` (joined while it still has those rows),
    then swapped into place - built under a temp name and dropped-then-renamed
    rather than the old table being renamed aside, so fix_runs' own foreign
    key (which names "fix_plans" literally) is never retargeted by SQLite's
    rename-tracking and left dangling once the old table is gone.
    """
    cols = {row[1] for row in conn.execute("PRAGMA table_info(fix_plans)")}
    if not cols or "project_key" in cols:
        return  # fresh db, or already migrated
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute(
        """
        CREATE TABLE fix_plans_new (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            issue_id      TEXT NOT NULL,
            project_key   TEXT,
            rule          TEXT,
            severity      TEXT,
            type          TEXT,
            message       TEXT,
            file_path     TEXT,
            line          INTEGER,
            explanation   TEXT,
            root_cause    TEXT,
            impact        TEXT,
            plan_json     TEXT,
            testing_notes TEXT,
            confidence    REAL,
            status        TEXT NOT NULL DEFAULT 'pending',
            feedback      TEXT,
            model         TEXT,
            created_at    TEXT
        )
        """
    )
    conn.execute(
        """
        INSERT INTO fix_plans_new
            (id, issue_id, project_key, rule, severity, type, message, file_path,
             line, explanation, root_cause, impact, plan_json, testing_notes,
             confidence, status, feedback, model, created_at)
        SELECT
            p.id, p.issue_id, i.project_key, i.rule, i.severity, i.type, i.message,
            i.file_path, i.line, p.explanation, p.root_cause, p.impact, p.plan_json,
            p.testing_notes, p.confidence, p.status, p.feedback, p.model, p.created_at
        FROM fix_plans p
        LEFT JOIN issues i ON i.id = p.issue_id
        """
    )
    conn.execute("DROP TABLE fix_plans")
    conn.execute("ALTER TABLE fix_plans_new RENAME TO fix_plans")


def init_db() -> None:
    with connect() as conn:
        conn.executescript(SCHEMA)
        _migrate_fix_plans(conn)
        for statement in (
            *_PROJECTS_MIGRATIONS, *_FIX_RUNS_MIGRATIONS, *_BATCHES_MIGRATIONS,
            *_DEPENDENCIES_MIGRATIONS,
        ):
            try:
                conn.execute(statement)
            except sqlite3.OperationalError:
                pass  # column already exists - a fresh CREATE TABLE already has it


def _rows(cursor: sqlite3.Cursor) -> list[dict[str, Any]]:
    return [dict(row) for row in cursor.fetchall()]


def strip_component_prefix(component: str, project_key: str = "") -> str:
    """Turn a Sonar component key into a repo-relative path.

    'my-project:src/main/java/Foo.java' -> 'src/main/java/Foo.java'

    Some Sonar project keys (e.g. 'com.unisys.InfoImage:Devops-110-SRC')
    contain a colon themselves, so the real project key - not just the first
    colon - must be stripped, or the leftover fragment still has one in it.
    """
    if project_key and component.startswith(project_key + ":"):
        return component[len(project_key) + 1 :]
    return component.split(":", 1)[1] if ":" in component else component


# --- projects ----------------------------------------------------------------


def upsert_projects(projects: Iterable[dict[str, str]]) -> None:
    rows = [{"key": p["key"], "name": p.get("name") or p["key"]} for p in projects]
    if not rows:
        return
    with connect() as conn:
        conn.executemany(
            "INSERT INTO projects (key, name) VALUES (:key, :name) "
            "ON CONFLICT(key) DO UPDATE SET name = excluded.name",
            rows,
        )


def list_projects() -> list[dict[str, Any]]:
    with connect() as conn:
        return _rows(conn.execute("SELECT * FROM projects ORDER BY name"))


def get_project(key: str) -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM projects WHERE key = ?", (key,)).fetchone()
        return dict(row) if row else None


def set_repo_path(key: str, repo_path: str) -> None:
    with connect() as conn:
        conn.execute("UPDATE projects SET repo_path = ? WHERE key = ?", (repo_path, key))


def set_default_branch(key: str, branch: str) -> None:
    """The real branch fix/batch work should always start from - never a
    stray branch a previous run happened to leave the clone checked out on.
    See `repo.safe_base_branch` for why this matters."""
    with connect() as conn:
        conn.execute("UPDATE projects SET default_branch = ? WHERE key = ?", (branch, key))


def mark_synced(key: str) -> None:
    with connect() as conn:
        conn.execute("UPDATE projects SET last_synced_at = ? WHERE key = ?", (now(), key))


# --- issues ------------------------------------------------------------------


def replace_issues(project_key: str, issues: list[dict[str, Any]]) -> int:
    """Sync is authoritative: upsert what we have and drop stale rows without children."""
    stamp = now()
    rows = [
        {
            "id": issue["key"],
            "project_key": project_key,
            "rule": issue.get("rule"),
            "severity": issue.get("severity"),
            "type": issue.get("type"),
            "status": issue.get("status"),
            "message": issue.get("message"),
            "component": issue.get("component"),
            "file_path": strip_component_prefix(
                issue.get("component") or "", issue.get("project") or ""
            ),
            "line": issue.get("line"),
            "raw_json": json.dumps(issue),
            "fetched_at": stamp,
        }
        for issue in issues
    ]
    with connect() as conn:
        # Upsert synced issues, preserving fix_plans/fix_runs history.
        if rows:
            conn.executemany(
                "INSERT INTO issues "
                "(id, project_key, rule, severity, type, status, message, "
                " component, file_path, line, raw_json, fetched_at) "
                "VALUES "
                "(:id, :project_key, :rule, :severity, :type, :status, :message, "
                " :component, :file_path, :line, :raw_json, :fetched_at) "
                "ON CONFLICT(id) DO UPDATE SET "
                "rule=excluded.rule, severity=excluded.severity, type=excluded.type, "
                "status=excluded.status, message=excluded.message, "
                "component=excluded.component, file_path=excluded.file_path, "
                "line=excluded.line, raw_json=excluded.raw_json, "
                "fetched_at=excluded.fetched_at",
                rows,
            )
        # Delete only stale issues with no analysis/fix history.
        synced_ids = [r["id"] for r in rows]
        placeholders = ", ".join("?" * len(synced_ids)) if synced_ids else "NULL"
        conn.execute(
            f"DELETE FROM issues WHERE project_key = ? AND id NOT IN ({placeholders}) "
            "AND id NOT IN (SELECT issue_id FROM fix_plans)",
            [project_key, *synced_ids],
        )
    return len(rows)


_ISSUE_COLUMNS = (
    "id, project_key, rule, severity, type, status, message, "
    "component, file_path, line, fetched_at"
)


def list_issues(
    project_key: str,
    severity: str | None = None,
    issue_type: str | None = None,
    *,
    include_raw: bool = False,
) -> list[dict[str, Any]]:
    # raw_json is the bulk of each row; lists only ship it when asked.
    columns = "*" if include_raw else _ISSUE_COLUMNS
    sql = f"SELECT {columns} FROM issues WHERE project_key = ?"
    params: list[Any] = [project_key]
    if severity:
        sql += " AND severity = ?"
        params.append(severity)
    if issue_type:
        sql += " AND type = ?"
        params.append(issue_type)
    with connect() as conn:
        issues = _rows(conn.execute(sql, params))
    issues.sort(
        key=lambda i: (SEVERITY_RANK.get(i["severity"], 9), i["file_path"] or "")
    )
    return issues


def get_issue(issue_id: str) -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM issues WHERE id = ?", (issue_id,)).fetchone()
        return dict(row) if row else None


def fix_status_by_issue(project_key: str) -> dict[str, dict[str, Any]]:
    """Latest fix_run with status 'applied' for each issue, keyed by issue_id.

    An issue counts as "fixed" as soon as its fix is committed locally, even
    before a PR is opened for it (`pr_url` is NULL until then) - that's the
    point at which it should stop showing up for re-analysis.
    """
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT p.issue_id AS issue_id, r.pr_url AS pr_url, r.branch AS branch,
                   r.build_status AS build_status, r.test_status AS test_status,
                   r.created_at AS created_at
            FROM fix_runs r
            JOIN fix_plans p ON p.id = r.plan_id
            WHERE p.project_key = ? AND r.status = 'applied'
            ORDER BY r.created_at ASC
            """,
            (project_key,),
        ).fetchall()
    return {row["issue_id"]: dict(row) for row in rows}


def issue_facets(project_key: str) -> dict[str, list[str]]:
    """Distinct severities and types present, for the UI filters."""
    with connect() as conn:
        severities = [
            r[0]
            for r in conn.execute(
                "SELECT DISTINCT severity FROM issues WHERE project_key = ? "
                "AND severity IS NOT NULL",
                (project_key,),
            )
        ]
        types = [
            r[0]
            for r in conn.execute(
                "SELECT DISTINCT type FROM issues WHERE project_key = ? "
                "AND type IS NOT NULL",
                (project_key,),
            )
        ]
    severities.sort(key=lambda s: SEVERITY_RANK.get(s, 9))
    return {"severities": severities, "types": sorted(types)}


# --- fix plans ---------------------------------------------------------------


def create_plan(issue: dict[str, Any], analysis: dict[str, Any], model: str) -> int:
    """`issue` is the normalized live-fetched issue (see core/issues.py) - its
    rule/severity/file/line/message are snapshotted here so this plan's
    history reads back the same regardless of what SonarQube says later."""
    with connect() as conn:
        cursor = conn.execute(
            "INSERT INTO fix_plans "
            "(issue_id, project_key, rule, severity, type, message, file_path, line, "
            " explanation, root_cause, impact, plan_json, "
            " testing_notes, confidence, status, model, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)",
            (
                issue["id"],
                issue.get("project_key"),
                issue.get("rule"),
                issue.get("severity"),
                issue.get("type"),
                issue.get("message"),
                issue.get("file_path"),
                issue.get("line"),
                analysis.get("explanation"),
                analysis.get("root_cause"),
                analysis.get("impact"),
                json.dumps(analysis.get("remediation_plan", [])),
                analysis.get("testing_notes"),
                analysis.get("confidence"),
                model,
                now(),
            ),
        )
        return int(cursor.lastrowid)


def get_plan(plan_id: int) -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM fix_plans WHERE id = ?", (plan_id,)
        ).fetchone()
        return dict(row) if row else None


def latest_plan(issue_id: str) -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM fix_plans WHERE issue_id = ? ORDER BY id DESC LIMIT 1",
            (issue_id,),
        ).fetchone()
        return dict(row) if row else None


def set_plan_status(plan_id: int, status: str, feedback: str | None = None) -> None:
    with connect() as conn:
        conn.execute(
            "UPDATE fix_plans SET status = ?, feedback = ? WHERE id = ?",
            (status, feedback, plan_id),
        )


# --- fix runs ----------------------------------------------------------------


def _run_row(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if not row:
        return None
    run = dict(row)
    run["artifacts"] = json.loads(run["artifacts"]) if run.get("artifacts") else []
    return run


def create_run(plan_id: int, fields: dict[str, Any]) -> int:
    with connect() as conn:
        cursor = conn.execute(
            "INSERT INTO fix_runs "
            "(plan_id, branch, base_branch, diff, changes_summary, commit_message, "
            " pr_title, pr_description, testing_suggestions, build_status, "
            " build_output, test_status, test_output, artifacts, status, error, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                plan_id,
                fields.get("branch"),
                fields.get("base_branch"),
                fields.get("diff"),
                fields.get("changes_summary"),
                fields.get("commit_message"),
                fields.get("pr_title"),
                fields.get("pr_description"),
                fields.get("testing_suggestions"),
                fields.get("build_status"),
                fields.get("build_output"),
                fields.get("test_status"),
                fields.get("test_output"),
                json.dumps(fields.get("artifacts") or []),
                fields.get("status", "generated"),
                fields.get("error"),
                now(),
            ),
        )
        return int(cursor.lastrowid)


def set_run_pr_url(run_id: int, url: str) -> None:
    with connect() as conn:
        conn.execute("UPDATE fix_runs SET pr_url = ? WHERE id = ?", (url, run_id))


def get_run(run_id: int) -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM fix_runs WHERE id = ?", (run_id,)).fetchone()
        return _run_row(row)


def latest_run(plan_id: int) -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM fix_runs WHERE plan_id = ? ORDER BY id DESC LIMIT 1",
            (plan_id,),
        ).fetchone()
        return _run_row(row)


# --- batches -----------------------------------------------------------------

_BATCH_JSON = ("selection", "steps", "artifacts")


def _batch_row(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if not row:
        return None
    batch = dict(row)
    for key in _BATCH_JSON:
        batch[key] = json.loads(batch[key]) if batch.get(key) else None
    return batch


def create_batch(project_key: str, selection: dict[str, Any]) -> int:
    stamp = now()
    with connect() as conn:
        cursor = conn.execute(
            "INSERT INTO batches (project_key, status, selection, steps, created_at, updated_at) "
            "VALUES (?, 'queued', ?, '[]', ?, ?)",
            (project_key, json.dumps(selection), stamp, stamp),
        )
        return int(cursor.lastrowid)


def update_batch(batch_id: int, **fields: Any) -> None:
    if not fields:
        return
    for key in _BATCH_JSON:
        if key in fields:
            fields[key] = json.dumps(fields[key])
    fields["updated_at"] = now()
    assignments = ", ".join(f"{key} = ?" for key in fields)
    with connect() as conn:
        conn.execute(
            f"UPDATE batches SET {assignments} WHERE id = ?",
            (*fields.values(), batch_id),
        )


def get_batch(batch_id: int) -> dict[str, Any] | None:
    with connect() as conn:
        return _batch_row(
            conn.execute("SELECT * FROM batches WHERE id = ?", (batch_id,)).fetchone()
        )


def list_batches(project_key: str, limit: int = 10) -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT id, project_key, status, branch, pr_url, error, created_at, updated_at "
            "FROM batches WHERE project_key = ? ORDER BY id DESC LIMIT ?",
            (project_key, limit),
        ).fetchall()
    return [dict(row) for row in rows]


# A batch at either status has a verified, committed fix - "ready" just
# hasn't been pushed as a PR yet. Both count as "resolved" for the plan
# screen: once a batch gets this far, its issues should stop showing up
# there to be fixed again.
_RESOLVED_BATCH_STATUSES = ("ready", "pr_open")


def resolved_batches(project_key: str) -> list[dict[str, Any]]:
    """Full rows (diff, build/test logs, artifacts, steps) for every batch
    of this project that reached a resolved state - what the "PR Created"
    tab shows in detail."""
    with connect() as conn:
        rows = conn.execute(
            f"SELECT * FROM batches WHERE project_key = ? AND status IN "
            f"({', '.join('?' * len(_RESOLVED_BATCH_STATUSES))}) ORDER BY id DESC",
            (project_key, *_RESOLVED_BATCH_STATUSES),
        ).fetchall()
    return [b for b in (_batch_row(row) for row in rows) if b]


def resolved_issue_ids(project_key: str) -> dict[str, int]:
    """issue_id -> the id of the resolved batch that fixed it, for every
    issue covered by a batch in `resolved_batches` - lets the plan screen
    exclude issues that are already fixed/PR'd instead of offering them
    again."""
    covered: dict[str, int] = {}
    for batch in resolved_batches(project_key):
        for step in batch.get("steps") or []:
            if step.get("kind") == "mechanical":
                for item in step.get("applied") or []:
                    covered.setdefault(item["issue_id"], batch["id"])
            else:
                for occ in step.get("occurrences") or []:
                    covered.setdefault(occ["issue_id"], batch["id"])
    return covered


def batch_status_by_issue(project_key: str) -> dict[str, dict[str, Any]]:
    """Same shape as `fix_status_by_issue`'s values (pr_url, branch,
    build_status, test_status, created_at), but for issues resolved through
    the batch flow instead of the single-issue one - the Issues page's
    "Fixed" tab must check both, or a batch-created PR never shows there."""
    covered: dict[str, dict[str, Any]] = {}
    for batch in resolved_batches(project_key):
        info = {
            "pr_url": batch.get("pr_url"),
            "branch": batch.get("branch"),
            "build_status": batch.get("build_status"),
            "test_status": batch.get("test_status"),
            "created_at": batch.get("created_at"),
            "batch_id": batch["id"],
        }
        for step in batch.get("steps") or []:
            if step.get("kind") == "mechanical":
                for item in step.get("applied") or []:
                    covered.setdefault(item["issue_id"], info)
            else:
                for occ in step.get("occurrences") or []:
                    covered.setdefault(occ["issue_id"], info)
    return covered


# --- dependencies --------------------------------------------------------


def replace_dependencies(project_key: str, findings: list[dict[str, Any]]) -> int:
    """A dependency scan is authoritative for this project: the previous
    scan's rows are dropped and replaced, same as `replace_issues`."""
    stamp = now()
    rows = [
        {
            "project_key": project_key,
            "ecosystem": f.get("ecosystem"),
            "package": f.get("package"),
            "current_version": f.get("current_version"),
            "fixed_version": f.get("fixed_version"),
            "severity": f.get("severity"),
            "advisory_id": f.get("advisory_id"),
            "manifest_path": f.get("manifest_path"),
            "is_direct": 1 if f.get("direct") else 0,
            "bump_kind": f.get("bump_kind"),
            "recommendation": f.get("recommendation"),
            "summary": f.get("summary"),
            "reason": f.get("reason"),
            "latest_version": f.get("latest_version"),
            "scanned_at": stamp,
        }
        for f in findings
    ]
    with connect() as conn:
        conn.execute("DELETE FROM dependencies WHERE project_key = ?", (project_key,))
        if rows:
            conn.executemany(
                "INSERT INTO dependencies "
                "(project_key, ecosystem, package, current_version, fixed_version, "
                " severity, advisory_id, manifest_path, is_direct, bump_kind, "
                " recommendation, summary, reason, latest_version, scanned_at) "
                "VALUES "
                "(:project_key, :ecosystem, :package, :current_version, :fixed_version, "
                " :severity, :advisory_id, :manifest_path, :is_direct, :bump_kind, "
                " :recommendation, :summary, :reason, :latest_version, :scanned_at)",
                rows,
            )
    return len(rows)


def list_dependencies(project_key: str) -> list[dict[str, Any]]:
    with connect() as conn:
        rows = _rows(
            conn.execute(
                "SELECT * FROM dependencies WHERE project_key = ? ORDER BY id", (project_key,)
            )
        )
        analyses = {
            (a["ecosystem"], a["package"], a["manifest_path"], a["current_version"]): a
            for a in _rows(
                conn.execute(
                    "SELECT * FROM dependency_analyses WHERE project_key = ?", (project_key,)
                )
            )
        }
    for row in rows:
        row["direct"] = bool(row.pop("is_direct"))
        found = analyses.get(
            (row["ecosystem"], row["package"], row["manifest_path"], row["current_version"])
        )
        row["analysis"] = None
        if found and found.get("result"):
            try:
                row["analysis"] = json.loads(found["result"])
                row["analysis"]["analyzed_at"] = found["analyzed_at"]
            except ValueError:
                pass
    rows.sort(key=lambda d: DEP_SEVERITY_RANK.get(d["severity"], 9))
    return rows


def get_dependency(dep_id: int) -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM dependencies WHERE id = ?", (dep_id,)).fetchone()
    if not row:
        return None
    found = dict(row)
    found["direct"] = bool(found.pop("is_direct"))
    return found


def save_dependency_analysis(project_key: str, dep: dict[str, Any], result: dict[str, Any]) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO dependency_analyses "
            "(project_key, ecosystem, package, manifest_path, current_version, result, analyzed_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                project_key, dep["ecosystem"], dep["package"], dep["manifest_path"],
                dep["current_version"], json.dumps(result), now(),
            ),
        )


def dependencies_scanned_at(project_key: str) -> str | None:
    with connect() as conn:
        row = conn.execute(
            "SELECT scanned_at FROM dependencies WHERE project_key = ? "
            "ORDER BY id DESC LIMIT 1",
            (project_key,),
        ).fetchone()
    return row["scanned_at"] if row else None
