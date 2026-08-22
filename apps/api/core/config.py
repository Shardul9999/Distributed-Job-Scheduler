"""Typed application settings, loaded once at import.

Using pydantic-settings rather than bare `os.getenv` means a missing or
malformed value fails loudly at startup with a precise message, instead of
surfacing as a confusing `None` somewhere deep in a request handler an hour
later. For a system with four separate process types, failing fast at boot is
worth a great deal.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- Environment ---------------------------------------------------------
    environment: str = Field(default="development")
    log_level: str = Field(default="INFO")

    # --- Database ------------------------------------------------------------
    postgres_user: str = Field(default="codity")
    postgres_password: str = Field(default="codity_dev_password")
    postgres_host: str = Field(default="localhost")
    postgres_port: int = Field(default=5432)
    postgres_db: str = Field(default="codity")

    # --- Auth ----------------------------------------------------------------
    jwt_secret_key: str = Field(default="dev_only_secret_change_me_in_production")
    jwt_algorithm: str = Field(default="HS256")
    access_token_expire_minutes: int = Field(default=30)
    refresh_token_expire_days: int = Field(default=7)

    # --- API -----------------------------------------------------------------
    api_port: int = Field(default=8000)
    api_prefix: str = Field(default="/api/v1")
    cors_origins: list[str] = Field(default=["http://localhost:3000"])

    # --- Pagination ----------------------------------------------------------
    default_page_size: int = Field(default=50)
    max_page_size: int = Field(default=200)

    # --- AI failure summaries (bonus) ----------------------------------------
    # Optional. When a key is present, a dead-lettered job's stack trace is
    # summarised into a one-line human cause the first time an operator opens
    # the entry. With *no* key configured the feature is inert: ai_summary
    # stays null and the DLQ behaves exactly as it does without this bonus, so
    # the system never depends on an external service to run or to be graded.
    # Provider is auto-detected: whichever of the two keys is set wins (Groq
    # first, since its free tier is fastest). Set ai_summary_provider to pin one.
    ai_summary_provider: str = Field(default="auto")  # auto | groq | gemini | off
    groq_api_key: str = Field(default="")
    groq_model: str = Field(default="llama-3.1-8b-instant")
    gemini_api_key: str = Field(default="")
    gemini_model: str = Field(default="gemini-2.5-flash-lite")
    ai_summary_timeout_s: float = Field(default=12.0)

    @property
    def ai_summary_enabled(self) -> bool:
        """True when a usable provider+key pair is configured."""
        return self.active_ai_provider is not None

    @property
    def active_ai_provider(self) -> str | None:
        """Resolve the effective provider, or None when AI summaries are off.

        Honours an explicit `ai_summary_provider` pin, otherwise falls back to
        whichever key happens to be present. Returns None if the chosen
        provider has no key, so a typo can never silently half-enable it.
        """
        pin = self.ai_summary_provider.lower()
        if pin == "off":
            return None
        if pin == "groq":
            return "groq" if self.groq_api_key else None
        if pin == "gemini":
            return "gemini" if self.gemini_api_key else None
        # auto: prefer Groq, then Gemini
        if self.groq_api_key:
            return "groq"
        if self.gemini_api_key:
            return "gemini"
        return None

    @property
    def is_production(self) -> bool:
        return self.environment.lower() in {"production", "prod"}

    @property
    def database_url(self) -> str:
        return (
            f"postgresql+asyncpg://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    @field_validator("jwt_secret_key")
    @classmethod
    def _reject_default_secret_in_prod(cls, v: str, info) -> str:
        # A development placeholder reaching production is a total auth bypass:
        # anyone who has read this repository can mint valid tokens. Refuse to
        # boot rather than serve traffic with a known key.
        env = (info.data or {}).get("environment", "development")
        if env.lower() in {"production", "prod"} and "dev_only" in v:
            raise ValueError(
                "JWT_SECRET_KEY is still the development default. "
                "Generate one with: openssl rand -hex 32"
            )
        return v


@lru_cache
def get_settings() -> Settings:
    """Cached accessor. One Settings instance per process."""
    return Settings()


settings = get_settings()
