"""Regression tests for the S125 (commented-out code) recipe.

    python scripts/recipes_test.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sonarfix.core.recipes import Target, _commented_code  # noqa: E402


def run(src: str, line: int, end: int | None = None) -> str | None:
    lines = src.splitlines(keepends=True)
    out = _commented_code(lines, Target(line, end or line, None, None, ""))
    return None if out is None else "".join(out)


def check(name: str, got, want) -> None:
    if got != want:
        print(f"[FAIL] {name}\n  want: {want!r}\n  got:  {got!r}")
        raise SystemExit(1)
    print(f"[OK]   {name}")


# Sonar flags only the `/*` line; the whole block must go, not just that line.
BLOCK = (
    "int a;\n"
    "/*\t\t\tstring sMsg;\n"
    "\t\t\tif (bEncrypt)\n"
    "\t\t\t\tsMsg = \"Input=\" + sInput;\n"
    "\t\t\tcu.LogMessage(sMsg);\n"
    "*/\n"
    "int b;\n"
)
check("multi-line block flagged on its first line", run(BLOCK, 2), "int a;\nint b;\n")
check("multi-line block flagged on its last line", run(BLOCK, 6), "int a;\nint b;\n")

check("single-line block comment", run("a;\n/* x = 1; */\nb;\n", 2), "a;\nb;\n")
check("code sharing the line is left alone", run("/* x */ int y;\n", 1), None)

RUN = "a;\n//\tsInput = sInput + \" \";\n//\twhile (x)\n//\t{\n//\t}\nb;\n"
check("adjacent commented code goes as one unit", run(RUN, 3), "a;\nb;\n")

PROSE = "a;\n// Retry three times before giving up\n//\tx = Foo();\nb;\n"
check("neighbouring prose comment is kept", run(PROSE, 3), "a;\n// Retry three times before giving up\nb;\n")

check("real code is never deleted", run("int x = 1;\n", 1), None)
check("unterminated block is declined", run("/* never closed\nint x;\n", 1), None)
print("all recipe tests passed")
