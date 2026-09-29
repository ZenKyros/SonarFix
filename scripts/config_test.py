"""Checks for the provider / engine / MCP configuration surface.

Pure configuration parsing and wiring - no network, no credentials, no model
calls. Complements scripts/smoke_test.py, which covers the workflow itself.

    python scripts/config_test.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sonarfix.core import claude_code, mcp, providers  # noqa: E402
from sonarfix.core.config import ConfigError, get_settings  # noqa: E402

# Settings the tool reads; cleared between cases so each one starts clean.
MANAGED = [
    "SONARFIX_LLM_PROVIDER",
    "SONARFIX_MODEL",
    "SONARFIX_LLM_BASE_URL",
    "SONARFIX_LLM_API_KEY",
    "SONARFIX_ENGINE",
    "SONARFIX_SONAR_MCP",
    "SONARFIX_SONAR_MCP_COMMAND",
    "SONARFIX_SONAR_MCP_ARGS",
    "SONARFIX_SONAR_MCP_URL",
    "ANTHROPIC_API_KEY",
    "SONAR_URL",
    "SONAR_TOKEN",
    "SONAR_ORG",
]

checks: list[tuple[str, bool, str]] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    checks.append((name, bool(condition), detail))


def configure(**env: str) -> None:
    """Install exactly this environment and drop the settings cache."""
    for key in MANAGED:
        os.environ.pop(key, None)
    os.environ.update(env)
    get_settings.cache_clear()


def raises_config_error(fn: Callable[[], Any]) -> str | None:
    """Return the error message when ConfigError is raised, else None."""
    try:
        fn()
    except ConfigError as exc:
        return str(exc)
    return None


BASE = {"SONAR_URL": "https://sonar.example.com", "SONAR_TOKEN": "tok"}


def main() -> int:
    # --- providers -----------------------------------------------------------

    configure(**BASE, ANTHROPIC_API_KEY="sk-test")
    settings = get_settings()
    check("default provider is anthropic", settings.provider == "anthropic")
    check("default model is claude-opus-5", settings.model == "claude-opus-5")
    check("anthropic config validates", raises_config_error(settings.require_llm) is None)
    check("anthropic is not flagged local", not settings.uses_local_llm)

    configure(**BASE, SONARFIX_LLM_PROVIDER="anthropic")
    message = raises_config_error(get_settings().require_llm)
    check(
        "anthropic without a key is rejected",
        message is not None and "ANTHROPIC_API_KEY" in message,
        str(message),
    )

    configure(**BASE, SONARFIX_LLM_PROVIDER="ollama")
    settings = get_settings()
    check("ollama gets a sensible default model", settings.model == "qwen2.5-coder:14b")
    check(
        "ollama needs no Anthropic key",
        raises_config_error(settings.require_llm) is None,
    )
    check("ollama is flagged local", settings.uses_local_llm)
    model = providers.build_model(max_tokens=512)
    check("ollama builds a ChatOllama", type(model).__name__ == "ChatOllama")

    configure(
        **BASE,
        SONARFIX_LLM_PROVIDER="openai-compatible",
        SONARFIX_MODEL="qwen2.5-coder",
        SONARFIX_LLM_BASE_URL="http://localhost:8000/v1",
    )
    model = providers.build_model(max_tokens=512)
    check("openai-compatible builds a ChatOpenAI", type(model).__name__ == "ChatOpenAI")
    check(
        "openai-compatible describes its endpoint",
        "localhost:8000" in providers.describe(),
        providers.describe(),
    )

    configure(**BASE, SONARFIX_LLM_PROVIDER="openai-compatible", SONARFIX_MODEL="m")
    message = raises_config_error(get_settings().require_llm)
    check(
        "openai-compatible without a base URL is rejected",
        message is not None and "SONARFIX_LLM_BASE_URL" in message,
        str(message),
    )

    configure(**BASE, SONARFIX_LLM_PROVIDER="not-a-provider")
    message = raises_config_error(get_settings)
    check(
        "an unknown provider is rejected by name",
        message is not None and "not-a-provider" in message,
        str(message),
    )

    # --- engines -------------------------------------------------------------

    configure(**BASE, ANTHROPIC_API_KEY="sk-test")
    check("default engine is deepagents", get_settings().engine == "deepagents")

    configure(**BASE, SONARFIX_ENGINE="claude-code", ANTHROPIC_API_KEY="sk-test")
    check("claude-code engine selectable", get_settings().engine == "claude-code")
    check(
        "claude-code accepts the anthropic provider",
        raises_config_error(claude_code._check_provider) is None,
    )

    configure(**BASE, SONARFIX_ENGINE="claude-code", SONARFIX_LLM_PROVIDER="ollama")
    message = raises_config_error(claude_code._check_provider)
    check(
        "claude-code rejects a local provider with a useful message",
        message is not None and "SONARFIX_ENGINE=deepagents" in message,
        str(message),
    )

    configure(**BASE, SONARFIX_ENGINE="nope")
    check("an unknown engine is rejected", raises_config_error(get_settings) is not None)

    # --- SonarQube MCP -------------------------------------------------------

    configure(**BASE)
    check("MCP defaults to off", not mcp.enabled())
    check("MCP off yields no server config", mcp.claude_code_config() == {})
    check("MCP off describes itself", mcp.describe() == "off")

    configure(**BASE, SONARFIX_SONAR_MCP="stdio")
    message = raises_config_error(get_settings)
    check(
        "stdio MCP without a command is rejected",
        message is not None and "SONARFIX_SONAR_MCP_COMMAND" in message,
        str(message),
    )

    configure(
        **BASE,
        SONAR_ORG="my-org",
        SONARFIX_SONAR_MCP="stdio",
        SONARFIX_SONAR_MCP_COMMAND="docker",
        SONARFIX_SONAR_MCP_ARGS='["run", "-i", "--rm", "mcp/sonarqube"]',
    )
    check("stdio MCP is enabled", mcp.enabled())
    config = mcp.claude_code_config()["sonarqube"]
    check("stdio MCP config uses stdio transport", config["type"] == "stdio")
    check("stdio MCP passes the command", config["command"] == "docker")
    check("stdio MCP parses JSON args", config["args"] == ["run", "-i", "--rm", "mcp/sonarqube"])
    check(
        "stdio MCP forwards Sonar credentials",
        config["env"]["SONARQUBE_URL"] == BASE["SONAR_URL"]
        and config["env"]["SONARQUBE_TOKEN"] == "tok"
        and config["env"]["SONARQUBE_ORG"] == "my-org",
    )
    connection = mcp._langchain_connection()
    check("langchain connection uses stdio", connection["transport"] == "stdio")
    check("langchain connection carries args", connection["args"][-1] == "mcp/sonarqube")

    configure(
        **BASE,
        SONARFIX_SONAR_MCP="stdio",
        SONARFIX_SONAR_MCP_COMMAND="docker",
        SONARFIX_SONAR_MCP_ARGS="run -i --rm mcp/sonarqube",
    )
    check(
        "shell-style args parse too",
        mcp.claude_code_config()["sonarqube"]["args"]
        == ["run", "-i", "--rm", "mcp/sonarqube"],
    )

    configure(**BASE, SONARFIX_SONAR_MCP="http")
    message = raises_config_error(get_settings)
    check(
        "http MCP without a URL is rejected",
        message is not None and "SONARFIX_SONAR_MCP_URL" in message,
        str(message),
    )

    configure(
        **BASE,
        SONARFIX_SONAR_MCP="http",
        SONARFIX_SONAR_MCP_URL="http://localhost:9000/mcp/",
    )
    config = mcp.claude_code_config()["sonarqube"]
    check("http MCP uses http transport", config["type"] == "http")
    check("http MCP strips the trailing slash", config["url"] == "http://localhost:9000/mcp")
    check(
        "http MCP sends a bearer token",
        config["headers"]["Authorization"] == "Bearer tok",
    )
    check(
        "langchain http connection is streamable",
        mcp._langchain_connection()["transport"] == "streamable_http",
    )

    configure(
        **BASE,
        SONARFIX_SONAR_MCP="stdio",
        SONARFIX_SONAR_MCP_COMMAND="docker",
        SONARFIX_SONAR_MCP_ARGS="[not json",
    )
    check("malformed JSON args are rejected", raises_config_error(get_settings) is not None)

    # --- report --------------------------------------------------------------

    width = max(len(name) for name, _, _ in checks)
    failed = 0
    for name, passed, detail in checks:
        print(f"{'PASS' if passed else 'FAIL'}  {name.ljust(width)}  {detail if not passed else ''}")
        failed += not passed

    print(f"\n{len(checks) - failed}/{len(checks)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
