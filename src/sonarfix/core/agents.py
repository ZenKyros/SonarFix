"""Structured output schemas and the two deepagents agents.

The analysis agent reads the repository; the fix agent writes to it. Both get
`ls`/`read_file`/`glob`/`grep` (plus `edit_file`/`write_file` for the fix
agent) and a `task` tool for delegating to subagents, sandboxed to the
repository via a `FilesystemBackend` in virtual mode - and, when configured,
the SonarQube MCP tools on top.

The model comes from `providers.build_model`, so these agents run on Claude or
on a local model without changing anything here.
"""

from __future__ import annotations

from collections.abc import Sequence
from functools import lru_cache
from pathlib import Path
from typing import Any

from deepagents import FilesystemPermission, create_deep_agent
from deepagents.backends import FilesystemBackend
from pydantic import BaseModel, Field

from .config import get_settings
from .providers import build_model

__all__ = [
    "FixOutcome",
    "IssueAnalysis",
    "PullRequestText",
    "analysis_agent",
    "build_model",
    "fix_agent",
    "load_prompt",
    "structured_response",
]


# --- structured outputs ------------------------------------------------------


class IssueAnalysis(BaseModel):
    """What the analysis agent must produce for step 4 and step 6."""

    explanation: str = Field(
        description="What the issue means, in plain language, for this code."
    )
    root_cause: str = Field(
        description="Why the code triggers this rule - the actual cause, not the rule text."
    )
    impact: str = Field(
        description="Concrete consequences: what can go wrong, for whom, and how likely."
    )
    severity_assessment: str = Field(
        description="Whether Sonar's severity is right for this codebase, and why."
    )
    related_files: list[str] = Field(
        default_factory=list,
        description="Repository paths of files that matter for the fix, including tests.",
    )
    remediation_plan: list[str] = Field(
        default_factory=list,
        description="Ordered, concrete steps to fix the issue.",
    )
    testing_notes: str = Field(
        description="How the fix should be verified, referring to real test files where they exist."
    )
    confidence: float = Field(
        ge=0.0, le=1.0, description="Confidence in this analysis and plan, 0 to 1."
    )


class FixOutcome(BaseModel):
    """What the fix agent reports after editing the working tree."""

    changes_summary: str = Field(
        description="What was changed and why. If the fix was not applied, say so and why."
    )
    files_changed: list[str] = Field(
        default_factory=list, description="Repository paths actually edited."
    )
    testing_suggestions: str = Field(
        description="What a reviewer should run or check for this change."
    )
    applied: bool = Field(
        description="True only if the approved plan was actually implemented in the files."
    )


class PullRequestText(BaseModel):
    """Step 9 and step 10, generated from the real diff."""

    commit_message: str = Field(
        description="Conventional-commit subject line, then a blank line, then a short body."
    )
    pr_title: str = Field(description="One-line pull request title.")
    pr_description: str = Field(
        description="Markdown PR body: what changed, why, how it was verified, and the Sonar issue reference."
    )


# --- prompts -----------------------------------------------------------------


@lru_cache(maxsize=4)
def load_prompt(name: str) -> str:
    return (get_settings().prompts_dir / f"{name}.md").read_text(encoding="utf-8")


# --- agents ------------------------------------------------------------------

# Everything under the repository root, as the agent's virtual filesystem sees it.
_ALL_PATHS = ["/**"]


def _backend(repo_path: str | Path) -> FilesystemBackend:
    return FilesystemBackend(root_dir=str(repo_path), virtual_mode=True)


def analysis_agent(repo_path: str | Path, tools: Sequence[Any] = ()) -> Any:
    """Read-only agent for steps 4-6: explain the issue and plan the fix."""
    return create_deep_agent(
        model=build_model(max_tokens=16_000, effort="high"),
        system_prompt=load_prompt("analyze"),
        backend=_backend(repo_path),
        tools=list(tools),
        permissions=[
            FilesystemPermission(operations=["write"], paths=_ALL_PATHS, mode="deny")
        ],
        response_format=IssueAnalysis,
        # Do not inherit the workflow graph's checkpointer.
        checkpointer=False,
        name="sonarfix-analyst",
    )


def fix_agent(repo_path: str | Path, tools: Sequence[Any] = ()) -> Any:
    """Read-write agent for step 8: implement the approved plan."""
    return create_deep_agent(
        model=build_model(max_tokens=32_000, effort="high"),
        system_prompt=load_prompt("fix"),
        backend=_backend(repo_path),
        tools=list(tools),
        response_format=FixOutcome,
        checkpointer=False,
        name="sonarfix-engineer",
    )


def structured_response(result: dict[str, Any], model: type[BaseModel]) -> dict[str, Any]:
    """Pull the validated structured output out of an agent's final state."""
    payload = result.get("structured_response")
    if payload is None:
        raise RuntimeError(
            "The agent finished without producing a structured response - a common "
            "symptom of a model that cannot reliably call tools or emit JSON. "
            "Last message: " + str(result.get("messages", [])[-1:])[:500]
        )
    if isinstance(payload, model):
        return payload.model_dump()
    return model.model_validate(payload).model_dump()
