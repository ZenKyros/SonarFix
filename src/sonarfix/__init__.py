""" SonarFix - AI-assisted remediation of SonarQube issues."""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

__version__ = "0.1.0"

UI_APP = Path(__file__).resolve().parent / "ui" / "app.py"


def _cmd_init_db(_: argparse.Namespace) -> int:
    from .core import store
    from .core.config import get_settings

    store.init_db()
    print(f"Database ready at {get_settings().db_path}")
    return 0


def _probe_llm() -> tuple[bool, str]:
    """One tiny generation, to prove the configured model actually answers."""
    from .core.providers import build_model

    try:
        model = build_model(max_tokens=64, effort="low")
        reply = model.invoke([{"role": "user", "content": "Reply with the word OK."}])
        text = getattr(reply, "text", None) or str(getattr(reply, "content", ""))
        return True, f"model replied: {text.strip()[:60]}"
    except Exception as exc:  # noqa: BLE001 - this is the diagnostic
        return False, f"{type(exc).__name__}: {exc}"


def _probe_mcp() -> tuple[bool, str]:
    """Start the configured MCP server and list the tools it offers."""
    from .core import mcp
    from .core.engine import run_sync

    async def _list() -> list[str]:
        async with mcp.sonar_tools() as tools:
            return [tool.name for tool in tools]

    try:
        names = run_sync(_list())
    except Exception as exc:  # noqa: BLE001 - this is the diagnostic
        return False, f"{type(exc).__name__}: {exc}"

    if not names:
        return True, "connected, but the server offered no tools"
    return True, f"{len(names)} tools: {', '.join(names[:6])}"


def _cmd_check(args: argparse.Namespace) -> int:
    """Confirm the environment is wired up before you rely on it."""
    from .core import engine, mcp
    from .core.config import ConfigError, get_settings

    try:
        settings = get_settings()
    except ConfigError as exc:
        print(f"Configuration is invalid:\n  ! {exc}")
        return 1

    ok = True

    print(f"Engine:     {settings.engine}")
    print(f"Provider:   {settings.provider}")
    print(f"Model:      {settings.model or '(not set)'}")
    if settings.uses_local_llm:
        print(f"Endpoint:   {settings.llm_base_url or 'provider default'}")
    print(f"Sonar MCP:  {mcp.describe()}")
    print(f"Database:   {settings.db_path}")
    print(f"Sonar URL:  {settings.sonar_url or '(not set)'}")
    print()

    # SonarQube REST API - always needed, MCP or not.
    try:
        settings.require_sonar()
    except ConfigError as exc:
        print(f"  !   {exc}")
        ok = False
    else:
        from .core import sonar

        try:
            projects = sonar.list_projects()
            print(f"  ok  SonarQube reachable, {len(projects)} projects visible.")
        except Exception as exc:  # noqa: BLE001
            print(f"  !   SonarQube call failed: {exc}")
            ok = False

    # LLM configuration.
    try:
        settings.require_llm()
        print(f"  ok  LLM configured: {engine.describe()}")
    except ConfigError as exc:
        print(f"  !   {exc}")
        ok = False

    if settings.engine == "claude-code":
        from .core import claude_code

        try:
            claude_code._require_sdk()
            print("  ok  claude-agent-sdk installed.")
        except ConfigError as exc:
            print(f"  !   {exc}")
            ok = False
        if not _which("claude"):
            print(
                "  !   The Claude Code CLI is not on PATH. Install it with:\n"
                "        npm install -g @anthropic-ai/claude-code"
            )
            ok = False
        else:
            print("  ok  Claude Code CLI found.")

    if args.live:
        passed, detail = _probe_llm()
        print(f"  {'ok ' if passed else '!  '} LLM probe - {detail}")
        ok = ok and passed

        if mcp.enabled():
            passed, detail = _probe_mcp()
            print(f"  {'ok ' if passed else '!  '} MCP probe - {detail}")
            ok = ok and passed
    elif mcp.enabled():
        print("  --  Sonar MCP configured; run with --live to connect and list tools.")

    print("\nReady." if ok else "\nNot ready - fix the items marked ! (see .env.example).")
    return 0 if ok else 1


def _which(name: str) -> str | None:
    import shutil

    return shutil.which(name)


def _cmd_api(args: argparse.Namespace) -> int:
    import uvicorn

    uvicorn.run(
        "sonarfix.api.main:app", host=args.host, port=args.port, reload=args.reload
    )
    return 0


def _cmd_ui(args: argparse.Namespace) -> int:
    return subprocess.call(
        [
            sys.executable,
            "-m",
            "streamlit",
            "run",
            str(UI_APP),
            "--server.port",
            str(args.port),
        ]
    )


def main() -> int:
    parser = argparse.ArgumentParser(prog="sonarfix", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init-db", help="Create the SQLite schema.").set_defaults(
        func=_cmd_init_db
    )

    check_parser = sub.add_parser(
        "check", help="Verify Sonar, LLM provider, engine and MCP configuration."
    )
    check_parser.add_argument(
        "--live",
        action="store_true",
        help="Also call the model and connect to the MCP server (costs a few tokens).",
    )
    check_parser.set_defaults(func=_cmd_check)

    api_parser = sub.add_parser("api", help="Run the FastAPI backend.")
    api_parser.add_argument("--host", default="127.0.0.1")
    api_parser.add_argument("--port", type=int, default=8000)
    api_parser.add_argument("--reload", action="store_true")
    api_parser.set_defaults(func=_cmd_api)

    ui_parser = sub.add_parser("ui", help="Run the Streamlit front end.")
    ui_parser.add_argument("--port", type=int, default=8501)
    ui_parser.set_defaults(func=_cmd_ui)

    args = parser.parse_args()
    return int(args.func(args) or 0)


if __name__ == "__main__":
    raise SystemExit(main())
