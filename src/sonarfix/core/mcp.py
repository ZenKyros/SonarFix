"""Optional SonarQube MCP server.

SonarSource ships an MCP server that exposes SonarQube to an agent as tools -
searching issues, fetching rule metadata, pulling raw source. With it enabled
the analyst can ask Sonar follow-up questions ("what else does this rule flag
in this project?") instead of working only from the one issue we pre-fetched.

SonarFix does not bundle or manage that server. You tell it how to reach one,
and the same configuration feeds both engines:

    deepagents  -> loaded as LangChain tools via langchain-mcp-adapters
    claude-code -> passed straight through as ClaudeAgentOptions.mcp_servers
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any, AsyncIterator

from .config import ConfigError, get_settings

if TYPE_CHECKING:  # pragma: no cover
    from langchain_core.tools import BaseTool

SERVER_NAME = "sonarqube"


def enabled() -> bool:
    return get_settings().sonar_mcp.enabled


def claude_code_config() -> dict[str, Any]:
    """`mcp_servers` mapping in the shape the Claude Agent SDK expects."""
    mcp = get_settings().sonar_mcp
    if not mcp.enabled:
        return {}
    if mcp.mode == "stdio":
        return {
            SERVER_NAME: {
                "type": "stdio",
                "command": mcp.command,
                "args": list(mcp.args),
                "env": dict(mcp.env),
            }
        }
    return {
        SERVER_NAME: {
            "type": "http",
            "url": mcp.url,
            "headers": {"Authorization": f"Bearer {mcp.env.get('SONARQUBE_TOKEN', '')}"},
        }
    }


def _langchain_connection() -> dict[str, Any]:
    """Connection dict in the shape langchain-mcp-adapters expects."""
    mcp = get_settings().sonar_mcp
    if mcp.mode == "stdio":
        return {
            "transport": "stdio",
            "command": mcp.command,
            "args": list(mcp.args),
            "env": dict(mcp.env),
        }
    return {
        "transport": "streamable_http",
        "url": mcp.url,
        "headers": {"Authorization": f"Bearer {mcp.env.get('SONARQUBE_TOKEN', '')}"},
    }


@asynccontextmanager
async def sonar_tools() -> AsyncIterator[list["BaseTool"]]:
    """Open one MCP session for the length of an agent run.

    Yields an empty list when the server is not configured, so callers do not
    need to branch. Holding the session open for the whole run means the
    server process starts once, not once per tool call.
    """
    settings = get_settings()
    if not settings.sonar_mcp.enabled:
        yield []
        return

    try:
        from langchain_mcp_adapters.client import MultiServerMCPClient
        from langchain_mcp_adapters.tools import load_mcp_tools
    except ImportError as exc:
        raise ConfigError(
            "langchain-mcp-adapters is not installed. Install the optional extra:\n"
            "    uv sync --extra mcp"
        ) from exc

    client = MultiServerMCPClient({SERVER_NAME: _langchain_connection()})
    async with client.session(SERVER_NAME) as session:
        yield await load_mcp_tools(session, server_name=SERVER_NAME)


def describe() -> str:
    mcp = get_settings().sonar_mcp
    if not mcp.enabled:
        return "off"
    if mcp.mode == "stdio":
        return f"stdio: {mcp.command} {' '.join(mcp.args)}".strip()
    return f"http: {mcp.url}"
