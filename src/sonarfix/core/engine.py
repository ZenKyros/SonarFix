"""Engine dispatch: the one seam the workflow graph talks to.

The graph asks for "analyse this" and "fix this" and does not care which
implementation answers. Two exist:

    SONARFIX_ENGINE=deepagents   LangGraph + deepagents on any provider (default)
    SONARFIX_ENGINE=claude-code  the Claude Agent SDK, driving the Claude Code CLI

Both get the SonarQube MCP tools when one is configured. Both are called from
synchronous graph nodes, so the async work is bridged here rather than turning
the whole workflow async.
"""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Coroutine, TypeVar

from pydantic import BaseModel

from . import claude_code, mcp
from .agents import (
    FixOutcome,
    IssueAnalysis,
    PullRequestText,
    analysis_agent,
    fix_agent,
    load_prompt,
    structured_response,
)
from .config import get_settings

T = TypeVar("T")

# Exploring a repository takes a lot of small steps.
RECURSION_LIMIT = 120


def run_sync(coro: Coroutine[Any, Any, T]) -> T:
    """Run a coroutine from sync code, whether or not a loop is already up.

    FastAPI runs our `def` endpoints in a worker thread, so normally there is
    no running loop and `asyncio.run` is correct. The thread fallback keeps
    this honest if it is ever called from async code.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)

    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()


# --- deepagents engine -------------------------------------------------------


async def _deepagents_run(
    repo_path: str | Path,
    task: str,
    schema: type[BaseModel],
    *,
    allow_writes: bool,
) -> dict[str, Any]:
    async with mcp.sonar_tools() as tools:
        agent = (
            fix_agent(repo_path, tools=tools)
            if allow_writes
            else analysis_agent(repo_path, tools=tools)
        )
        result = await agent.ainvoke(
            {"messages": [{"role": "user", "content": task}]},
            config={"recursion_limit": RECURSION_LIMIT},
        )
    return structured_response(result, schema)


# --- public API --------------------------------------------------------------


def run_analysis(repo_path: str | Path, task: str) -> dict[str, Any]:
    """Steps 4-6: explain the issue and plan the fix. Must not write."""
    if get_settings().engine == "claude-code":
        return run_sync(
            claude_code.run(
                prompt=task,
                system_prompt=load_prompt("analyze"),
                repo_path=repo_path,
                schema=IssueAnalysis,
                allow_writes=False,
            )
        )
    return run_sync(
        _deepagents_run(repo_path, task, IssueAnalysis, allow_writes=False)
    )


def run_fix(repo_path: str | Path, task: str) -> dict[str, Any]:
    """Step 8: implement the approved plan in the working tree."""
    if get_settings().engine == "claude-code":
        return run_sync(
            claude_code.run(
                prompt=task,
                system_prompt=load_prompt("fix"),
                repo_path=repo_path,
                schema=FixOutcome,
                allow_writes=True,
            )
        )
    return run_sync(_deepagents_run(repo_path, task, FixOutcome, allow_writes=True))


PR_SYSTEM_PROMPT = (
    "You write the commit message and pull request text for a change that has "
    "already been made. The diff is in the prompt; describe only what it "
    "actually does. Do not read the repository, do not use tools, and do not "
    "claim any test or build was run."
)


def run_pr_text(repo_path: str | Path, task: str) -> dict[str, Any]:
    """Steps 9-10: commit message and PR description, written from the diff.

    Routed through the engine like the other steps, so a claude-code setup with
    no API key can finish the workflow.
    """
    if get_settings().engine == "claude-code":
        return run_sync(
            claude_code.run(
                prompt=task,
                system_prompt=PR_SYSTEM_PROMPT,
                repo_path=repo_path,
                schema=PullRequestText,
                allow_writes=False,
                # Everything it needs is in the prompt; no exploration wanted.
                max_turns=4,
            )
        )

    from .providers import build_model

    model = build_model(max_tokens=8_000, effort="medium").with_structured_output(
        PullRequestText
    )
    result = model.invoke([{"role": "user", "content": task}])
    if isinstance(result, PullRequestText):
        return result.model_dump()
    return PullRequestText.model_validate(result).model_dump()


def describe() -> str:
    """One line summarising the active engine, provider and MCP wiring."""
    from . import providers

    settings = get_settings()
    engine = (
        claude_code.describe()
        if settings.engine == "claude-code"
        else f"deepagents / {providers.describe()}"
    )
    return f"{engine} | sonar-mcp: {mcp.describe()}"
