"""A persistent activity log: everything SonarFix and the AI do, in order.

Each event is one row (when, what kind, which batch/project, a short title and
optional detail). Batch runs, AI sessions, builds and reviewer decisions all
write here, and the UI reads it back as a timeline. Logging never raises - a
broken log must not break a fix.
"""

from __future__ import annotations

import contextlib
import contextvars
import json
import logging
import time
from typing import Any, Iterator

from . import store

_log = logging.getLogger("sonarfix.activity")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS activity (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    at           TEXT NOT NULL,
    level        TEXT NOT NULL,
    kind         TEXT NOT NULL,
    title        TEXT NOT NULL,
    detail       TEXT,
    batch_id     INTEGER,
    project_key  TEXT,
    duration_ms  INTEGER,
    data_json    TEXT
);
CREATE INDEX IF NOT EXISTS idx_activity_batch ON activity(batch_id);
CREATE INDEX IF NOT EXISTS idx_activity_project ON activity(project_key);
"""

# Which batch/project the current work belongs to, so deep code (the AI
# engine) can log without every function passing ids down.
_context: contextvars.ContextVar[dict[str, Any]] = contextvars.ContextVar("activity_ctx", default={})
_ready = False

MAX_DETAIL = 6000


def _ensure() -> None:
    global _ready
    if not _ready:
        with store.connect() as conn:
            conn.executescript(_SCHEMA)
        _ready = True


@contextlib.contextmanager
def scope(**ids: Any) -> Iterator[None]:
    """Attach batch_id / project_key to every event logged inside."""
    token = _context.set({**_context.get(), **{k: v for k, v in ids.items() if v is not None}})
    try:
        yield
    finally:
        _context.reset(token)


def log(
    kind: str,
    title: str,
    detail: str | None = None,
    *,
    level: str = "info",
    duration_ms: int | None = None,
    data: dict[str, Any] | None = None,
    **ids: Any,
) -> None:
    """Record one event. `kind` groups it (batch, ai, build, review, system)."""
    try:
        _ensure()
        ctx = {**_context.get(), **{k: v for k, v in ids.items() if v is not None}}
        text = (detail or "")[:MAX_DETAIL] or None
        with store.connect() as conn:
            conn.execute(
                "INSERT INTO activity (at, level, kind, title, detail, batch_id, project_key, duration_ms, data_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    store.now(), level, kind, title, text, ctx.get("batch_id"), ctx.get("project_key"),
                    duration_ms, json.dumps(data, default=str) if data else None,
                ),
            )
        _log.log(
            {"error": logging.ERROR, "warn": logging.WARNING}.get(level, logging.INFO),
            "[%s] %s%s", kind, title, f" - {text.splitlines()[0]}" if text else "",
        )
    except Exception:  # noqa: BLE001 - logging must never break the work it describes
        _log.exception("could not record activity event")


@contextlib.contextmanager
def timed(kind: str, title: str, detail: str | None = None, **ids: Any) -> Iterator[None]:
    """Log '<title> started', then '<title> finished/failed' with the duration."""
    log(kind, f"{title} - started", detail, **ids)
    began = time.monotonic()
    try:
        yield
    except Exception as exc:
        log(kind, f"{title} - failed", str(exc), level="error",
            duration_ms=int((time.monotonic() - began) * 1000), **ids)
        raise
    log(kind, f"{title} - finished", level="info",
        duration_ms=int((time.monotonic() - began) * 1000), **ids)


def list_events(
    *, batch_id: int | None = None, project_key: str | None = None,
    after_id: int = 0, limit: int = 300,
) -> list[dict[str, Any]]:
    """Events oldest-first. Pass `after_id` to fetch only what is new."""
    _ensure()
    where, args = ["id > ?"], [after_id]
    if batch_id is not None:
        where.append("batch_id = ?")
        args.append(batch_id)
    if project_key:
        where.append("project_key = ?")
        args.append(project_key)
    with store.connect() as conn:
        rows = conn.execute(
            f"SELECT * FROM (SELECT * FROM activity WHERE {' AND '.join(where)} "
            "ORDER BY id DESC LIMIT ?) ORDER BY id ASC",
            (*args, max(1, min(limit, 1000))),
        ).fetchall()
    events = []
    for row in rows:
        item = dict(row)
        raw = item.pop("data_json", None)
        item["data"] = json.loads(raw) if raw else None
        events.append(item)
    return events
