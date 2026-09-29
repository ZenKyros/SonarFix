"""LLM provider factory.

One function, three backends. Swapping Claude for a local model is an
environment change, not a code change.

    SONARFIX_LLM_PROVIDER=anthropic          -> Claude (default)
    SONARFIX_LLM_PROVIDER=ollama             -> Ollama on your machine
    SONARFIX_LLM_PROVIDER=openai-compatible  -> vLLM, LM Studio, llama.cpp, LiteLLM...

Caveat worth knowing before you switch: both agents depend on reliable tool
calling and JSON-schema output. Frontier models do this well; small local
models often do not, and the failure looks like an agent that never calls a
tool or returns unparseable output. Prefer a local model advertised as
tool-calling capable, and give it a large context window.
"""

from __future__ import annotations

from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel

from .config import ConfigError, get_settings

# Local models need to be told to reserve room; 8k output on a 4k default
# context silently truncates everything the agent reads.
_DEFAULT_LOCAL_CONTEXT = 32_768


def _missing(package: str, extra: str) -> ConfigError:
    return ConfigError(
        f"{package} is not installed. Install the optional extra with:\n"
        f"    uv sync --extra {extra}"
    )


def _anthropic(max_tokens: int, effort: str) -> BaseChatModel:
    try:
        from langchain_anthropic import ChatAnthropic
    except ImportError as exc:  # pragma: no cover - base dependency
        raise _missing("langchain-anthropic", "anthropic") from exc

    settings = get_settings()
    kwargs: dict[str, Any] = {
        "model": settings.model,
        "max_tokens": max_tokens,
        # Turns on adaptive thinking for us on models that support it.
        "reasoning_effort": effort,
        "max_retries": 3,
        "timeout": 600.0,
    }

    if settings.anthropic_api_key:
        kwargs["api_key"] = settings.anthropic_api_key
    elif settings.anthropic_auth_token:
        # ChatAnthropic stores the key as a SecretStr and always forwards it, so
        # leaving it unset sends an empty `X-Api-Key` header rather than falling
        # through to the SDK's OAuth chain. Authenticate with the header instead,
        # which is the path langchain-anthropic documents for bearer tokens.
        kwargs["default_headers"] = {
            "Authorization": f"Bearer {settings.anthropic_auth_token}",
            "anthropic-beta": "oauth-2025-04-20",
        }

    return ChatAnthropic(**kwargs)


def _ollama(max_tokens: int) -> BaseChatModel:
    try:
        from langchain_ollama import ChatOllama
    except ImportError as exc:
        raise _missing("langchain-ollama", "local") from exc

    settings = get_settings()
    kwargs: dict[str, Any] = {
        "model": settings.model,
        "num_predict": max_tokens,
        "num_ctx": _DEFAULT_LOCAL_CONTEXT,
        # Deterministic-ish output; these agents are not meant to be creative.
        "temperature": 0.0,
    }
    if settings.llm_base_url:
        kwargs["base_url"] = settings.llm_base_url
    return ChatOllama(**kwargs)


def _openai_compatible(max_tokens: int) -> BaseChatModel:
    try:
        from langchain_openai import ChatOpenAI
    except ImportError as exc:
        raise _missing("langchain-openai", "local") from exc

    settings = get_settings()
    return ChatOpenAI(
        model=settings.model,
        base_url=settings.llm_base_url,
        # Most local servers ignore the key but the client insists on one.
        api_key=settings.llm_api_key or "not-needed",
        max_tokens=max_tokens,
        temperature=0.0,
        timeout=600.0,
        max_retries=3,
    )


def build_model(max_tokens: int = 16_000, effort: str = "high") -> BaseChatModel:
    """The chat model for the configured provider.

    `effort` only means something on Anthropic models; the local providers
    ignore it.
    """
    settings = get_settings()
    settings.require_llm()

    if settings.provider == "anthropic":
        return _anthropic(max_tokens, effort)
    if settings.provider == "ollama":
        return _ollama(max_tokens)
    if settings.provider == "openai-compatible":
        return _openai_compatible(max_tokens)

    raise ConfigError(f"Unknown provider: {settings.provider}")


def describe() -> str:
    """One line for `sonarfix check` and the UI footer."""
    settings = get_settings()
    if settings.provider == "anthropic":
        return f"anthropic / {settings.model}"
    target = settings.llm_base_url or "http://localhost:11434"
    return f"{settings.provider} / {settings.model} @ {target}"
