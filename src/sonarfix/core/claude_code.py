"""The Claude Agent SDK (Claude Code as a library) as an alternative engine.

This is a different product from the Claude API the `deepagents` engine uses:
it drives the Claude Code harness, with its own built-in Read/Edit/Grep/Glob
tools, its own context management, and native MCP support.

Prerequisite: the Claude Code CLI must be installed and on PATH, because the
SDK spawns it.

    npm install -g @anthropic-ai/claude-code

Enable with SONARFIX_ENGINE=claude-code.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from .config import ConfigError, get_settings
from . import mcp

_JSON_BLOCK = re.compile(r"\{.*\}", re.DOTALL)

# Read-only for analysis; the fix agent additionally gets the edit tools.
READ_TOOLS = ["Read", "Grep", "Glob"]
WRITE_TOOLS = ["Read", "Grep", "Glob", "Edit", "Write"]
NEVER = ["Bash", "WebFetch", "WebSearch", "NotebookEdit", "KillShell", "BashOutput"]


def _require_sdk() -> Any:
    try:
        import claude_agent_sdk
    except ImportError as exc:
        raise ConfigError(
            "claude-agent-sdk is not installed. Install the optional extra:\n"
            "    uv sync --extra claude-code"
        ) from exc
    return claude_agent_sdk


def _check_provider() -> None:
    settings = get_settings()
    if settings.provider != "anthropic":
        raise ConfigError(
            f"SONARFIX_ENGINE=claude-code runs the Claude Code CLI, which needs "
            f"SONARFIX_LLM_PROVIDER=anthropic (currently {settings.provider!r}). "
            "To use a local model, set SONARFIX_ENGINE=deepagents."
        )


def _extract(result: Any, schema: type[BaseModel]) -> dict[str, Any]:
    """Prefer the CLI's structured output; fall back to JSON in the text."""
    payload = getattr(result, "structured_output", None)
    if isinstance(payload, dict):
        return schema.model_validate(payload).model_dump()

    text = getattr(result, "result", None) or ""
    match = _JSON_BLOCK.search(text)
    if match:
        try:
            return schema.model_validate(json.loads(match.group(0))).model_dump()
        except (json.JSONDecodeError, ValueError):
            pass

    raise RuntimeError(
        "The Claude Code run finished without usable structured output. "
        f"Last text: {text[:500]}"
    )


async def run(
    *,
    prompt: str,
    system_prompt: str,
    repo_path: str | Path,
    schema: type[BaseModel],
    allow_writes: bool,
    max_turns: int = 80,
) -> dict[str, Any]:
    """Run one Claude Code session against the repository and parse its output."""
    sdk = _require_sdk()
    _check_provider()
    settings = get_settings()

    options = sdk.ClaudeAgentOptions(
        model=settings.model,
        cwd=str(repo_path),
        system_prompt=system_prompt,
        allowed_tools=(WRITE_TOOLS if allow_writes else READ_TOOLS)
        + (["mcp__sonarqube"] if mcp.enabled() else []),
        disallowed_tools=NEVER + ([] if allow_writes else ["Edit", "Write"]),
        permission_mode=settings.claude_code_permission_mode,
        # Do not inherit the machine's CLAUDE.md or user settings - the prompts
        # in prompts/ are the whole instruction set.
        setting_sources=[],
        mcp_servers=mcp.claude_code_config(),
        max_turns=max_turns,
        output_format={"type": "json_schema", "schema": schema.model_json_schema()},
    )

    final: Any = None
    try:
        async for message in sdk.query(prompt=prompt, options=options):
            if isinstance(message, sdk.ResultMessage):
                final = message
    except sdk.CLINotFoundError as exc:
        raise ConfigError(
            "The Claude Code CLI was not found, and SONARFIX_ENGINE=claude-code "
            "needs it. Install it with:\n"
            "    npm install -g @anthropic-ai/claude-code\n"
            "or switch back with SONARFIX_ENGINE=deepagents."
        ) from exc

    if final is None:
        raise RuntimeError("The Claude Code session ended without a result message.")
    if getattr(final, "is_error", False):
        raise RuntimeError(
            f"Claude Code reported an error: {getattr(final, 'result', '')[:500]}"
        )

    return _extract(final, schema)


def describe() -> str:
    settings = get_settings()
    return f"claude-code / {settings.model} ({settings.claude_code_permission_mode})"
