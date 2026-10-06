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

CREATE TABLE IF NOT EXISTS fix_plans (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    issue_id      TEXT NOT NULL REFERENCES issues(id),
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
    error         TEXT,
    created_at    TEXT,
    updated_at    TEXT
);

CREATE INDEX IF NOT EXISTS idx_batches_project ON batches(project_key);
"""

# Severity order used for sorting in the UI (worst first).
SEVERITY_RANK = {
    "BLOCKER": 0,
    "CRITICAL": 1,
    "MAJOR": 2,
    "MINOR": 3,
    "INFO": 4,
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


_FIX_RUNS_MIGRATIONS = (
    "ALTER TABLE fix_runs ADD COLUMN base_branch TEXT",
    "ALTER TABLE fix_runs ADD COLUMN pr_title TEXT",
    "ALTER TABLE fix_runs ADD COLUMN pr_url TEXT",
    "ALTER TABLE fix_runs ADD COLUMN build_status TEXT",
    "ALTER TABLE fix_runs ADD COLUMN build_output TEXT",
)


def init_db() -> None:
    with connect() as conn:
        conn.executescript(SCHEMA)
        for statement in _FIX_RUNS_MIGRATIONS:
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


def create_plan(issue_id: str, analysis: dict[str, Any], model: str) -> int:
    with connect() as conn:
        cursor = conn.execute(
            "INSERT INTO fix_plans "
            "(issue_id, explanation, root_cause, impact, plan_json, "
            " testing_notes, confidence, status, model, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)",
            (
                issue_id,
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


def create_run(plan_id: int, fields: dict[str, Any]) -> int:
    with connect() as conn:
        cursor = conn.execute(
            "INSERT INTO fix_runs "
            "(plan_id, branch, base_branch, diff, changes_summary, commit_message, "
            " pr_title, pr_description, testing_suggestions, build_status, "
            " build_output, status, error, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
        return dict(row) if row else None


def latest_run(plan_id: int) -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM fix_runs WHERE plan_id = ? ORDER BY id DESC LIMIT 1",
            (plan_id,),
        ).fetchone()
        return dict(row) if row else None


# --- batches -----------------------------------------------------------------

_BATCH_JSON = ("selection", "steps")


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
