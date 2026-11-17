"""Runtime settings, loaded from environment / .env (pydantic-settings).

Secrets (``CHAT_API_KEY``, provider keys) are ``SecretStr``: never logged,
never echoed in validation errors.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import AliasChoices, Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parent.parent

Provider = Literal["groq", "openrouter"]

# OpenAI-compatible base URL per provider.
PROVIDER_BASE_URLS: dict[str, str] = {
    "openrouter": "https://openrouter.ai/api/v1",
    "groq": "https://api.groq.com/openai/v1",
}


class LeakSettings(BaseSettings):
    """Project-specific leak markers (env prefix ``LEAK_``), kept out of git.

    Comma-separated lists, set in .env. Generic checks (private IPs,
    loopback, canary, ``.mp4``) live in code; these add the internal ports
    and storage / file names that must never appear in an answer.
    """

    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_prefix="LEAK_",
        extra="ignore",
        hide_input_in_errors=True,
    )

    internal_ports: str = ""
    internal_names: str = ""

    def ports(self) -> list[str]:
        """Return the configured port numbers."""
        return [p.strip() for p in self.internal_ports.split(",")
                if p.strip().isdigit()]

    def names(self) -> list[str]:
        """Return the configured internal names (literal strings)."""
        return [n.strip() for n in self.internal_names.split(",") if n.strip()]


class AppSettings(BaseSettings):
    """Service settings. ``CHAT_API_KEY`` is required and never logged."""

    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        extra="ignore",
        # Never echo loaded values (secrets from .env) in validation errors.
        hide_input_in_errors=True,
    )

    # ---- API ----------------------------------------------------------------
    chat_api_key: SecretStr = Field(validation_alias="CHAT_API_KEY")
    session_max_turns: int = 10
    session_ttl_s: float = 30 * 60
    # Upper bound for one /v1/chat call, including the LLM request.
    chat_timeout_s: float = 45.0
    # Retry-After sent on 429 when the provider gives none.
    default_retry_after_s: int = 30
    # Serve /docs and /openapi.json. Local development only; staging and
    # production must leave this false (docker-compose forces it off).
    docs_enabled: bool = False
    # Cleaned corpus (Markdown pages from the ingestion tooling).
    corpus_dir: Path = PROJECT_ROOT / "data" / "clean"

    # ---- LLM provider -------------------------------------------------------
    llm_provider: Provider = "groq"
    # OpenRouter key keeps its original variable name (API_KEY).
    openrouter_api_key: SecretStr | None = Field(
        default=None,
        validation_alias=AliasChoices("OPENROUTER_API_KEY", "API_KEY"),
    )
    groq_api_key: SecretStr | None = Field(
        default=None, validation_alias=AliasChoices("GROQ_API_KEY", "groq")
    )
    # Empty = use the provider default from PROVIDER_BASE_URLS.
    llm_base_url: str = ""
    # Model used for answers.
    chat_model: str = "openai/gpt-oss-20b"
    temperature: float = 0.2
    max_tokens: int = 700
    reasoning_effort: str = "low"
    request_timeout_s: float = 90.0
    # Client-side throttle per model (Groq free tier: 30 RPM, 8K TPM).
    rate_limit_rpm: int = 30
    # Fail fast: 1 attempt means a provider 429 becomes our 429 immediately.
    llm_max_retries: int = 1

    # ---- Content ------------------------------------------------------------
    # Canonical public site used in citations and the URL whitelist.
    # Unconfirmed: inferred from the site's sales@trendwise.io address.
    public_base_url: str = "https://trendwise.io"
    sales_email: str = "sales@trendwise.io"

    @model_validator(mode="after")
    def _apply_provider_defaults(self) -> AppSettings:
        self.llm_base_url = (self.llm_base_url
                             or PROVIDER_BASE_URLS[self.llm_provider])
        return self

    @property
    def api_key(self) -> SecretStr:
        """Return the key for the selected provider."""
        key = (self.groq_api_key if self.llm_provider == "groq"
               else self.openrouter_api_key)
        if key is None:
            raise ValueError(f"No API key configured for {self.llm_provider}")
        return key
