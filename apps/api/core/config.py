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
