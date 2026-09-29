"""Mechanical fix recipes: deterministic, zero-token transforms for Sonar rules.

A recipe edits only what Sonar pointed at. Every recipe first checks that the
exact text at the issue's `textRange` is what the rule implies - so a stale
issue (the clone moved on since the scan) is reported as "not applicable" and
handed to a human or the AI path instead of corrupting code.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable

# One edit is the list of file lines after the change (keepends=True lines).
Lines = list[str]


@dataclass(frozen=True)
class Target:
    """Where Sonar says the problem is (1-based lines, 0-based columns)."""

    start_line: int
    end_line: int
    start_col: int | None
    end_col: int | None
    message: str


@dataclass(frozen=True)
class Recipe:
    id: str
    rules: tuple[str, ...]
    title: str
    why_safe: str
    before: str
    after: str
    apply: Callable[[Lines, Target], Lines | None]


def _eol(line: str) -> str:
    """The line ending of `line`, so edits never convert CRLF files to LF."""
    return line[len(line.rstrip("\r\n")):]


def _token(lines: Lines, t: Target) -> tuple[str, str] | None:
    """(line_text_without_eol, token) for a single-line target, else None."""
    if t.start_line != t.end_line or t.start_col is None or t.end_col is None:
        return None
    if not 1 <= t.start_line <= len(lines):
        return None
    text = lines[t.start_line - 1].rstrip("\r\n")
    if t.end_col > len(text):
        return None
    return text, text[t.start_col:t.end_col]


def _replace_token(lines: Lines, t: Target, new: str) -> Lines:
    text, _ = _token(lines, t)  # type: ignore[misc]
    idx = t.start_line - 1
    out = list(lines)
    out[idx] = text[: t.start_col] + new + text[t.end_col :] + _eol(lines[idx])
    return out


# --- recipes -----------------------------------------------------------------


def _logging_exception(lines: Lines, t: Target) -> Lines | None:
    found = _token(lines, t)
    if not found or found[1] != "error":
        return None
    return _replace_token(lines, t, "exception")


def _bare_except(lines: Lines, t: Target) -> Lines | None:
    if not 1 <= t.start_line <= len(lines):
        return None
    line = lines[t.start_line - 1]
    match = re.match(r"^(\s*)except\s*:(.*)$", line.rstrip("\r\n"))
    if not match:
        return None
    out = list(lines)
    out[t.start_line - 1] = f"{match.group(1)}except Exception:{match.group(2)}{_eol(line)}"
    return out


def _unused_js_import(lines: Lines, t: Target) -> Lines | None:
    found = _token(lines, t)
    if not found:
        return None
    text, token = found
    named = re.search(r"'([^']+)'", t.message)
    if not token or (named and named.group(1) != token):
        return None

    idx = t.start_line - 1
    stripped = text.strip()
    out = list(lines)
    # Specifier on a line of its own inside a multi-line import list.
    if stripped in (token, f"{token},"):
        del out[idx]
        return out

    before, after = text[: t.start_col], text[t.end_col :]
    # Prefer eating the separator that belongs to this specifier.
    if re.search(r",\s*$", before):
        before = re.sub(r",\s*$", "", before)
    elif re.match(r"^\s*,\s*", after):
        after = re.sub(r"^\s*,\s*", "", after, count=1)
    new_line = before + after

    # `import { } from "x"` or `import Foo, { } from` left behind: tidy it.
    new_line = re.sub(r",\s*\{\s*\}", "", new_line)
    if re.match(r"^\s*import\s*\{\s*\}\s*from\s", new_line):
        del out[idx]
        return out
    out[idx] = new_line + _eol(lines[idx])
    return out


def _unused_using(lines: Lines, t: Target) -> Lines | None:
    if not 1 <= t.start_line <= len(lines):
        return None
    text = lines[t.start_line - 1].strip()
    # Directive only - never a `using (...)` statement or `using var`.
    if not re.match(r"^using\s+(static\s+)?[\w.]+(\s*=\s*[\w.<>]+)?\s*;$", text):
        return None
    out = list(lines)
    del out[t.start_line - 1]
    return out


def _unused_java_import(lines: Lines, t: Target) -> Lines | None:
    if not 1 <= t.start_line <= len(lines):
        return None
    if not re.match(r"^\s*import\s+(static\s+)?[\w.*]+\s*;\s*$", lines[t.start_line - 1]):
        return None
    out = list(lines)
    del out[t.start_line - 1]
    return out


def _rethrow(lines: Lines, t: Target) -> Lines | None:
    if not 1 <= t.start_line <= len(lines):
        return None
    line = lines[t.start_line - 1]
    new = re.sub(r"\bthrow\s+[A-Za-z_]\w*\s*;", "throw;", line, count=1)
    if new == line:
        return None
    out = list(lines)
    out[t.start_line - 1] = new
    return out


_COMMENT_LINE = re.compile(r"^\s*(//|#|/\*|\*|\*/)")


def _commented_code(lines: Lines, t: Target) -> Lines | None:
    if not (1 <= t.start_line <= t.end_line <= len(lines)):
        return None
    block = lines[t.start_line - 1 : t.end_line]
    # Only ever delete lines that are purely comments.
    if not all(_COMMENT_LINE.match(line) for line in block):
        return None
    out = list(lines)
    del out[t.start_line - 1 : t.end_line]
    return out


def _keyword_lowercase(lines: Lines, t: Target) -> Lines | None:
    found = _token(lines, t)
    if not found or not found[1].isalpha() or found[1] == found[1].lower():
        return None
    return _replace_token(lines, t, found[1].lower())


RECIPES: tuple[Recipe, ...] = (
    Recipe(
        id="logging-exception",
        rules=("python:S8572",),
        title="Log with logger.exception() inside except blocks",
        why_safe="Same log level and message; only adds the traceback.",
        before='except Exception as e:\n    logger.error(f"Load failed: {e}")',
        after='except Exception as e:\n    logger.exception(f"Load failed: {e}")',
        apply=_logging_exception,
    ),
    Recipe(
        id="bare-except",
        rules=("python:S5754",),
        title="Replace bare except: with except Exception:",
        why_safe="Stops swallowing SystemExit and KeyboardInterrupt; handler body unchanged.",
        before="try:\n    load()\nexcept:\n    pass",
        after="try:\n    load()\nexcept Exception:\n    pass",
        apply=_bare_except,
    ),
    Recipe(
        id="unused-js-import",
        rules=("javascript:S1128", "typescript:S1128"),
        title="Remove the unused import specifier",
        why_safe="Removes only the named, unreferenced binding Sonar reported.",
        before='import { useState, useCallback } from "react";',
        after='import { useState } from "react";',
        apply=_unused_js_import,
    ),
    Recipe(
        id="unused-using",
        rules=("csharpsquid:S1128",),
        title="Remove the unnecessary using directive",
        why_safe="Deletes a namespace directive nothing in the file uses.",
        before="using System;\nusing System.Text;\n\nclass A { }",
        after="using System;\n\nclass A { }",
        apply=_unused_using,
    ),
    Recipe(
        id="unused-java-import",
        rules=("java:S1128",),
        title="Remove the unused import",
        why_safe="Deletes an import statement nothing in the file uses.",
        before="import java.util.List;\nimport java.util.Map;",
        after="import java.util.List;",
        apply=_unused_java_import,
    ),
    Recipe(
        id="rethrow-preserve-stack",
        rules=("csharpsquid:S3445",),
        title="Rethrow with throw; to keep the stack trace",
        why_safe="Same exception propagates; the original stack trace is preserved.",
        before="catch (Exception ex)\n{\n    Log(ex);\n    throw ex;\n}",
        after="catch (Exception ex)\n{\n    Log(ex);\n    throw;\n}",
        apply=_rethrow,
    ),
    Recipe(
        id="commented-out-code",
        rules=("csharpsquid:S125", "java:S125", "javascript:S125", "typescript:S125", "python:S125"),
        title="Delete commented-out code",
        why_safe="Removes comment lines only; version control keeps the history.",
        before="int total = 0;\n// total = Recalculate(items);\nreturn total;",
        after="int total = 0;\nreturn total;",
        apply=_commented_code,
    ),
    Recipe(
        id="keyword-lowercase",
        rules=("powershelldre:S8642",),
        title="Lower-case the PowerShell keyword",
        why_safe="PowerShell keywords are case-insensitive; this is cosmetic.",
        before="Param(\n    [string]$Name\n)",
        after="param(\n    [string]$Name\n)",
        apply=_keyword_lowercase,
    ),
)

_BY_RULE = {rule: recipe for recipe in RECIPES for rule in recipe.rules}


def recipe_for(rule: str | None) -> Recipe | None:
    return _BY_RULE.get(rule or "")


def target_from(issue: dict, raw: dict) -> Target:
    text_range = raw.get("textRange") or {}
    line = issue.get("line") or text_range.get("startLine") or 0
    return Target(
        start_line=int(text_range.get("startLine") or line),
        end_line=int(text_range.get("endLine") or line),
        start_col=text_range.get("startOffset"),
        end_col=text_range.get("endOffset"),
        message=issue.get("message") or "",
    )
