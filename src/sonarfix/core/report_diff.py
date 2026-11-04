"""Turn a unified diff into a readable, per-file HTML view for the reports.

A raw `git diff` is hard to scan: no colour, no per-file totals, and hunk
headers like `@@ -134,7 +134,8 @@` that mean nothing to most readers. This
parses the diff into files and lines so the report can show, for each file,
what was added, what was removed, and where - with the usual red/green.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass, field

_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@\s*(.*)$")


@dataclass
class DiffLine:
    kind: str  # "add" | "del" | "ctx"
    old_no: int | None
    new_no: int | None
    text: str


@dataclass
class DiffHunk:
    context: str
    lines: list[DiffLine] = field(default_factory=list)


@dataclass
class FileDiff:
    path: str
    status: str = "modified"  # modified | added | deleted | renamed | binary
    old_path: str | None = None
    hunks: list[DiffHunk] = field(default_factory=list)

    @property
    def added(self) -> int:
        return sum(1 for h in self.hunks for l in h.lines if l.kind == "add")

    @property
    def removed(self) -> int:
        return sum(1 for h in self.hunks for l in h.lines if l.kind == "del")


def _strip_prefix(path: str) -> str:
    path = path.strip().strip('"')
    return path[2:] if path[:2] in ("a/", "b/") else path


def parse_diff(diff: str) -> list[FileDiff]:
    files: list[FileDiff] = []
    current: FileDiff | None = None
    hunk: DiffHunk | None = None
    old_no = new_no = 0

    for raw in (diff or "").splitlines():
        if raw.startswith("diff --git "):
            parts = raw.split(" b/", 1)
            path = _strip_prefix(parts[1]) if len(parts) == 2 else _strip_prefix(raw.split()[-1])
            current = FileDiff(path=path)
            files.append(current)
            hunk = None
        elif current is None:
            # A plain `diff -u` (no git header): start a file at its '---/+++' pair.
            if raw.startswith("+++ "):
                current = FileDiff(path=_strip_prefix(raw[4:].split("\t")[0]))
                files.append(current)
        elif hunk is None and raw.startswith("new file mode"):
            current.status = "added"
        elif hunk is None and raw.startswith("deleted file mode"):
            current.status = "deleted"
        elif hunk is None and raw.startswith("rename from "):
            current.status, current.old_path = "renamed", raw[len("rename from "):]
        elif raw.startswith("Binary files") or raw.startswith("GIT binary patch"):
            current.status = "binary"
        elif raw.startswith("+++ ") and hunk is None:
            target = _strip_prefix(raw[4:].split("\t")[0])
            if target != "/dev/null" and target:
                current.path = target
        elif raw.startswith("--- ") and hunk is None:
            continue
        else:
            match = _HUNK_RE.match(raw)
            if match:
                old_no, new_no = int(match.group(1)), int(match.group(2))
                hunk = DiffHunk(context=match.group(3).strip())
                current.hunks.append(hunk)
            elif hunk is not None:
                if raw.startswith("+"):
                    hunk.lines.append(DiffLine("add", None, new_no, raw[1:]))
                    new_no += 1
                elif raw.startswith("-"):
                    hunk.lines.append(DiffLine("del", old_no, None, raw[1:]))
                    old_no += 1
                elif raw.startswith("\\"):
                    continue  # "\ No newline at end of file"
                else:
                    hunk.lines.append(DiffLine("ctx", old_no, new_no, raw[1:] if raw.startswith(" ") else raw))
                    old_no += 1
                    new_no += 1
    return files


def totals(files: list[FileDiff]) -> tuple[int, int, int]:
    """(files changed, lines added, lines removed)."""
    return len(files), sum(f.added for f in files), sum(f.removed for f in files)


_STATUS_LABEL = {
    "added": "New file",
    "deleted": "Deleted file",
    "renamed": "Renamed",
    "binary": "Binary file",
}


def _esc(value: object) -> str:
    return html.escape(str(value))


def _file_html(f: FileDiff, open_by_default: bool) -> str:
    label = _STATUS_LABEL.get(f.status)
    badges = ""
    if label:
        badges += f'<span class="dv-tag">{label}</span>'
    if f.added:
        badges += f'<span class="dv-add-count">+{f.added}</span>'
    if f.removed:
        badges += f'<span class="dv-del-count">&minus;{f.removed}</span>'
    renamed = f' <span class="muted">(from {_esc(f.old_path)})</span>' if f.old_path else ""

    if not f.hunks:
        body = '<p class="dv-empty">No line changes to show.</p>'
    else:
        rows = []
        for h in f.hunks:
            if f.hunks[0] is not h or h.context:
                where = f"Around: {_esc(h.context)}" if h.context else "&hellip;"
                rows.append(f'<tr class="dv-gap"><td colspan="3">{where}</td></tr>')
            for line in h.lines:
                sign = {"add": "+", "del": "&minus;", "ctx": ""}[line.kind]
                num = line.new_no if line.kind != "del" else line.old_no
                rows.append(
                    f'<tr class="dv-{line.kind}"><td class="dv-no">{num}</td>'
                    f'<td class="dv-sign">{sign}</td><td class="dv-code">{_esc(line.text) or "&nbsp;"}</td></tr>'
                )
        body = f'<table class="dv-table">{"".join(rows)}</table>'

    return (
        f'<details class="dv-file"{" open" if open_by_default else ""}>'
        f'<summary><span class="dv-path">{_esc(f.path)}</span>{renamed}'
        f'<span class="dv-badges">{badges}</span></summary>{body}</details>'
    )


def render_diff(diff: str) -> str:
    """HTML for a whole diff: one collapsible, colour-coded block per file."""
    files = parse_diff(diff)
    if not files:
        return '<p class="dv-empty">No code changes were recorded.</p>'
    many = len(files) > 6
    return (
        '<div class="dv">'
        + "".join(_file_html(f, open_by_default=not many or i == 0) for i, f in enumerate(files))
        + "</div>"
    )


DIFF_CSS = """
  .dv { margin-top: 8px; }
  .dv-file { border: 1px solid #e2e4e8; border-radius: 8px; margin-bottom: 10px; background: #fff; overflow: hidden; }
  .dv-file > summary {
    cursor: pointer; padding: 9px 14px; background: #f3f5f9; display: flex; gap: 10px;
    align-items: center; flex-wrap: wrap; list-style: none;
  }
  .dv-file > summary::-webkit-details-marker { display: none; }
  .dv-file > summary::before { content: "\\25B8"; color: #7d8696; }
  .dv-file[open] > summary::before { content: "\\25BE"; }
  .dv-path { font-family: ui-monospace, SFMono-Regular, Consolas, monospace; font-size: 0.85rem; font-weight: 600; word-break: break-all; }
  .dv-badges { margin-left: auto; display: flex; gap: 6px; align-items: center; }
  .dv-add-count { color: #0a6b34; background: #d1f4de; font-weight: 700; font-size: 0.75rem; padding: 1px 8px; border-radius: 999px; }
  .dv-del-count { color: #a3231f; background: #fbdada; font-weight: 700; font-size: 0.75rem; padding: 1px 8px; border-radius: 999px; }
  .dv-tag { color: #236a97; background: #e6f0f7; font-weight: 600; font-size: 0.72rem; padding: 1px 8px; border-radius: 999px; }
  .dv-table { width: 100%; border-collapse: collapse; font-family: ui-monospace, SFMono-Regular, Consolas, monospace; font-size: 0.8rem; }
  .dv-table td { padding: 1px 8px; vertical-align: top; white-space: pre-wrap; word-break: break-word; }
  .dv-no { width: 1%; text-align: right; color: #9aa1ad; user-select: none; background: #fafbfc; border-right: 1px solid #eceef1; }
  .dv-sign { width: 1%; text-align: center; user-select: none; font-weight: 700; }
  .dv-add { background: #e6f7ec; } .dv-add .dv-sign { color: #0a6b34; } .dv-add .dv-no { background: #d3f0dd; }
  .dv-del { background: #fdecec; } .dv-del .dv-sign { color: #a3231f; } .dv-del .dv-no { background: #f9d9d9; }
  .dv-gap td { background: #f3f5f9; color: #7d8696; font-size: 0.75rem; padding: 3px 12px; }
  .dv-empty { color: #888; font-style: italic; padding: 8px 14px; margin: 0; }
"""
