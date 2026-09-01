"""Application settings.

All configuration comes from environment variables or a local ``.env`` file. Nothing is
hard-coded: there is no default for ``DATABASE_URL`` and no default credential anywhere.
Secrets use ``SecretStr`` so they cannot be printed or logged by accident.

Settings are loaded lazily through :func:`get_settings` so that merely importing a module
never fails on a missing environment.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any, Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from ..domain.errors import ConfigurationError

_CSV_FIELDS = ("notify_default_providers", "allowed_url_schemes")


def _split_csv(raw: str) -> tuple[str, ...]:
    return tuple(item.strip().lower() for item in raw.split(",") if item.strip())


class Settings(BaseSettings):
    """Effective runtime configuration.

    Comma-separated list options (``NOTIFY_DEFAULT_PROVIDERS``, ``ALLOWED_URL_SCHEMES``)
    are stored as raw strings and exposed as tuples via properties. Declaring them as
    ``list[str]`` would make pydantic-settings demand JSON in the environment, which is
    awkward in a ``.env`` file and in Docker Compose.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- Database ---------------------------------------------------------------
    database_url: str = Field(description="PostgreSQL DSN. Required; no default.")
    db_pool_size: int = Field(default=5, ge=1, le=100)
    db_max_overflow: int = Field(default=10, ge=0, le=100)
    db_echo: bool = False
    db_connect_timeout_seconds: int = Field(
        default=5,
        ge=2,
        le=60,
        description=(
            "Bound on establishing a connection. Without it, an unreachable host makes "
            "the readiness probe hang instead of reporting 'not ready'."
        ),
    )

    # Reserved for a future Celery/RQ JobQueue implementation. Unused in v1.
    redis_url: str | None = None

    # --- Logging ----------------------------------------------------------------
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    log_format: Literal["json", "console"] = "json"

    # --- Scheduling -------------------------------------------------------------
    check_interval_seconds: int = Field(default=3600, ge=60, le=86_400 * 7)
    reconcile_interval_seconds: int = Field(default=60, ge=10, le=3600)

    # --- Outbound HTTP ----------------------------------------------------------
    http_timeout_seconds: int = Field(default=25, ge=1, le=120)
    http_max_retries: int = Field(default=3, ge=0, le=10)
    http_user_agent: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
    http_accept_language: str = "en-IN,en;q=0.9"
    http_max_response_bytes: int = Field(default=5_000_000, ge=10_000)

    # --- Politeness towards stores ----------------------------------------------
    store_min_interval_seconds: float = Field(default=5.0, ge=0.0, le=600.0)
    fetch_jitter_seconds: float = Field(default=3.0, ge=0.0, le=60.0)
    store_failure_threshold: int = Field(
        default=5, ge=1, le=100, description="Consecutive failures before a store is backed off."
    )
    store_circuit_reset_seconds: int = Field(default=900, ge=30, le=86_400)

    # --- Playwright -------------------------------------------------------------
    playwright_enabled: bool = True
    playwright_headless: bool = True
    playwright_nav_timeout_seconds: int = Field(default=25, ge=1, le=120)

    # --- API --------------------------------------------------------------------
    api_key: SecretStr | None = Field(
        default=None, description="If set, required as X-API-Key on mutating endpoints."
    )
    api_allow_anonymous_reads: bool = True
    api_max_page_size: int = Field(default=100, ge=1, le=1000)
    api_default_page_size: int = Field(default=20, ge=1, le=1000)

    # --- URL safety -------------------------------------------------------------
    block_private_addresses: bool = Field(
        default=True, description="SSRF guard: reject URLs resolving to private ranges."
    )
    allowed_url_schemes: str = "https,http"
    max_url_length: int = Field(default=2048, ge=32, le=8192)

    # --- Notifications ----------------------------------------------------------
    notify_default_providers: str = "console"
    notification_timeout_seconds: int = Field(
        default=10,
        ge=1,
        le=120,
        description=(
            "Per-provider delivery timeout. Bounded because delivery currently runs "
            "inside the check's transaction; a hanging provider would hold it open."
        ),
    )

    smtp_host: str | None = None
    smtp_port: int = Field(default=587, ge=1, le=65_535)
    smtp_username: str | None = None
    smtp_password: SecretStr | None = None
    smtp_from: str | None = None
    smtp_to: str | None = None
    smtp_use_tls: bool = True

    telegram_bot_token: SecretStr | None = None
    telegram_chat_id: str | None = None

    webhook_url: str | None = None

    # --- Derived views ----------------------------------------------------------

    @property
    def notification_providers(self) -> tuple[str, ...]:
        return _split_csv(self.notify_default_providers)

    @property
    def url_schemes(self) -> tuple[str, ...]:
        return _split_csv(self.allowed_url_schemes)

    @field_validator("database_url")
    @classmethod
    def _require_postgres(cls, value: str) -> str:
        """Normalise the DSN onto the psycopg 3 driver and reject non-PostgreSQL URLs."""
        value = value.strip()
        if not value:
            raise ValueError("DATABASE_URL must not be empty")
        if value.startswith("postgres://"):
            value = "postgresql://" + value[len("postgres://") :]
        if value.startswith("postgresql://"):
            value = "postgresql+psycopg://" + value[len("postgresql://") :]
        if not value.startswith("postgresql+psycopg://"):
            raise ValueError(
                "DATABASE_URL must be a PostgreSQL DSN "
                "(e.g. postgresql+psycopg://user:pass@host:5432/db)"
            )
        return value

    @field_validator("allowed_url_schemes")
    @classmethod
    def _known_schemes(cls, value: str) -> str:
        schemes = _split_csv(value)
        if not schemes:
            raise ValueError("ALLOWED_URL_SCHEMES must list at least one scheme")
        unknown = set(schemes) - {"http", "https"}
        if unknown:
            raise ValueError(f"unsupported URL scheme(s): {', '.join(sorted(unknown))}")
        return value

    def redacted(self) -> dict[str, Any]:
        """Settings as a plain dict with every secret replaced by a marker.

        Safe to print (``product-tracker config``) and to log.
        """
        out: dict[str, Any] = {}
        for name in type(self).model_fields:
            value = getattr(self, name)
            if isinstance(value, SecretStr):
                out[name] = "***set***"
            elif name == "database_url":
                out[name] = mask_dsn_password(value)
            elif name in _CSV_FIELDS:
                out[name] = list(_split_csv(value))
            else:
                out[name] = value
        return out


def mask_dsn_password(dsn: str) -> str:
    """Replace the password in a DSN so it can be displayed or logged."""
    if "@" not in dsn or "://" not in dsn:
        return dsn
    scheme, _, rest = dsn.partition("://")
    credentials, _, host = rest.rpartition("@")
    if ":" not in credentials:
        return dsn
    user, _, _password = credentials.partition(":")
    return f"{scheme}://{user}:***@{host}"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Load and cache settings, translating pydantic errors into our own type."""
    try:
        # Field values are supplied by the environment, not by the caller.
        return Settings()
    except Exception as exc:  # pydantic ValidationError, or a bad .env
        raise ConfigurationError(f"invalid configuration: {exc}") from exc


def reset_settings_cache() -> None:
    """Drop the cached settings. Used by tests that patch the environment."""
    get_settings.cache_clear()
