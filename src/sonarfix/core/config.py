"""Single source of truth for configuration. Everything comes from the environment."""

from __future__ import annotations

import json
import os
import shlex
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[3]

load_dotenv(PROJECT_ROOT / ".env")

# Which LLM backend serves the agents.
PROVIDERS = ("anthropic", "ollama", "openai-compatible")
# Which agent implementation runs the analyse and fix steps.
ENGINES = ("deepagents", "claude-code")
# How the SonarQube MCP server is reached, if at all.
MCP_MODES = ("off", "stdio", "http")


class ConfigError(RuntimeError):
    """Raised when a required setting is missing or contradictory."""


@dataclass(frozen=True)
class SonarMcpSettings:
    """Optional SonarQube MCP server, shared by both engines."""

    mode: str = "off"
    command: str = ""
    args: tuple[str, ...] = ()
    url: str = ""
    env: dict[str, str] = field(default_factory=dict)

    @property
    def enabled(self) -> bool:
        return self.mode != "off"

    def validate(self) -> None:
        if self.mode == "stdio" and not self.command:
            raise ConfigError(
                "SONARFIX_SONAR_MCP=stdio needs SONARFIX_SONAR_MCP_COMMAND."
            )
        if self.mode == "http" and not self.url:
            raise ConfigError("SONARFIX_SONAR_MCP=http needs SONARFIX_SONAR_MCP_URL.")


@dataclass(frozen=True)
class Settings:
    # SonarQube
    sonar_url: str
    sonar_token: str
    sonar_org: str
    # LLM
    provider: str
    model: str
    llm_base_url: str
    llm_api_key: str
    anthropic_api_key: str
    anthropic_auth_token: str
    # agent engine
    engine: str
    claude_code_permission_mode: str
    # local
    db_path: Path
    prompts_dir: Path
    api_url: str
    context_radius: int
    # MCP
    sonar_mcp: SonarMcpSettings
    # Bitbucket (push + pull requests)
    bitbucket_token: str = ""
    bitbucket_username: str = ""
    bitbucket_url: str = ""
    # Upper bound on occurrences sent to one AI session, to cap token spend.
    max_ai_occurrences: int = 20

    def require_sonar(self) -> None:
        missing = [
            name
            for name, value in (("SONAR_URL", self.sonar_url), ("SONAR_TOKEN", self.sonar_token))
            if not value
        ]
        if missing:
            raise ConfigError(f"{' and '.join(missing)} must be set (see .env.example).")

    def require_llm(self) -> None:
        """Each provider needs something different; say which."""
        if self.provider == "anthropic":
            if self.engine == "claude-code":
                # The Claude Code CLI owns authentication: an API key if you set
                # one, otherwise the login it already holds (including a Claude
                # enterprise subscription). Nothing for us to require here.
                return
            if not (self.anthropic_api_key or self.anthropic_auth_token):
                raise ConfigError(
                    "Set ANTHROPIC_API_KEY, or ANTHROPIC_AUTH_TOKEN for OAuth.\n"
                    "Note: a Claude Code login (~/.claude/.credentials.json) is not "
                    "readable by the Python SDK - to use that, set "
                    "SONARFIX_ENGINE=claude-code."
                )
        elif self.provider == "ollama":
            if not self.model:
                raise ConfigError(
                    "SONARFIX_MODEL must name an Ollama model, e.g. qwen2.5-coder:14b."
                )
        elif self.provider == "openai-compatible":
            if not self.llm_base_url:
                raise ConfigError(
                    "SONARFIX_LLM_BASE_URL must point at your server, "
                    "e.g. http://localhost:8000/v1."
                )
            if not self.model:
                raise ConfigError("SONARFIX_MODEL must name a model on that server.")
        else:
            raise ConfigError(
                f"Unknown SONARFIX_LLM_PROVIDER={self.provider!r}. "
                f"Choose one of: {', '.join(PROVIDERS)}."
            )

    @property
    def uses_local_llm(self) -> bool:
        return self.provider in {"ollama", "openai-compatible"}


def _int_env(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


def _choice_env(name: str, default: str, allowed: tuple[str, ...]) -> str:
    value = (os.environ.get(name) or default).strip().lower()
    if value not in allowed:
        raise ConfigError(
            f"{name}={value!r} is not valid. Choose one of: {', '.join(allowed)}."
        )
    return value


def _args_env(name: str) -> tuple[str, ...]:
    """Accept either a JSON list or a shell-style string."""
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return ()
    if raw.startswith("["):
        try:
            return tuple(str(item) for item in json.loads(raw))
        except json.JSONDecodeError as exc:
            raise ConfigError(f"{name} is not valid JSON: {exc}") from exc
    return tuple(shlex.split(raw))


def _sonar_mcp_settings(sonar_url: str, sonar_token: str, sonar_org: str) -> SonarMcpSettings:
    mode = _choice_env("SONARFIX_SONAR_MCP", "off", MCP_MODES)
    # The MCP server authenticates to Sonar with the same credentials we use.
    env = {"SONARQUBE_URL": sonar_url, "SONARQUBE_TOKEN": sonar_token}
    if sonar_org:
        env["SONARQUBE_ORG"] = sonar_org
    settings = SonarMcpSettings(
        mode=mode,
        command=(os.environ.get("SONARFIX_SONAR_MCP_COMMAND") or "").strip(),
        args=_args_env("SONARFIX_SONAR_MCP_ARGS"),
        url=(os.environ.get("SONARFIX_SONAR_MCP_URL") or "").strip().rstrip("/"),
        env=env,
    )
    settings.validate()
    return settings


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    db_raw = os.environ.get("SONARFIX_DB", "data/sonarfix.db").strip()
    db_path = Path(db_raw)
    if not db_path.is_absolute():
        db_path = PROJECT_ROOT / db_path

    provider = _choice_env("SONARFIX_LLM_PROVIDER", "anthropic", PROVIDERS)
    default_model = {
        "anthropic": "claude-opus-5",
        "ollama": "qwen2.5-coder:14b",
        "openai-compatible": "",
    }[provider]

    sonar_url = os.environ.get("SONAR_URL", "").strip().rstrip("/")
    sonar_token = os.environ.get("SONAR_TOKEN", "").strip()
    sonar_org = os.environ.get("SONAR_ORG", "").strip()

    return Settings(
        sonar_url=sonar_url,
        sonar_token=sonar_token,
        sonar_org=sonar_org,
        provider=provider,
        model=(os.environ.get("SONARFIX_MODEL") or default_model).strip(),
        llm_base_url=(os.environ.get("SONARFIX_LLM_BASE_URL") or "").strip().rstrip("/"),
        llm_api_key=(os.environ.get("SONARFIX_LLM_API_KEY") or "").strip(),
        anthropic_api_key=os.environ.get("ANTHROPIC_API_KEY", "").strip(),
        anthropic_auth_token=os.environ.get("ANTHROPIC_AUTH_TOKEN", "").strip(),
        engine=_choice_env("SONARFIX_ENGINE", "deepagents", ENGINES),
        claude_code_permission_mode=(
            os.environ.get("SONARFIX_CLAUDE_CODE_PERMISSION_MODE") or "acceptEdits"
        ).strip(),
        db_path=db_path,
        prompts_dir=PROJECT_ROOT / "prompts",
        api_url=os.environ.get("SONARFIX_API_URL", "http://127.0.0.1:8000")
        .strip()
        .rstrip("/"),
        context_radius=_int_env("SONARFIX_CONTEXT_RADIUS", 40),
        sonar_mcp=_sonar_mcp_settings(sonar_url, sonar_token, sonar_org),
        bitbucket_token=os.environ.get("BITBUCKET_TOKEN", "").strip(),
        bitbucket_username=os.environ.get("BITBUCKET_USERNAME", "").strip(),
        bitbucket_url=os.environ.get("BITBUCKET_URL", "").strip().rstrip("/"),
        max_ai_occurrences=_int_env("SONARFIX_MAX_AI_OCCURRENCES", 20),
    )
