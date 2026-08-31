"""Settings loading, DSN normalisation, and secret redaction."""

from __future__ import annotations

import pytest

from product_tracker.core.config import Settings, get_settings, mask_dsn_password
from product_tracker.domain.errors import ConfigurationError


def _settings(**overrides: str) -> Settings:
    base = {"database_url": "postgresql://u:p@localhost:5432/db"}
    return Settings(**{**base, **overrides})  # type: ignore[arg-type]


class TestDatabaseUrl:
    @pytest.mark.parametrize(
        "given",
        [
            "postgres://u:p@localhost:5432/db",
            "postgresql://u:p@localhost:5432/db",
            "postgresql+psycopg://u:p@localhost:5432/db",
        ],
    )
    def test_normalises_onto_psycopg_driver(self, given: str) -> None:
        assert _settings(database_url=given).database_url == (
            "postgresql+psycopg://u:p@localhost:5432/db"
        )

    @pytest.mark.parametrize(
        "given",
        ["sqlite:///local.db", "mysql://u:p@localhost/db", "not-a-url", ""],
    )
    def test_rejects_non_postgres(self, given: str) -> None:
        with pytest.raises(ValueError):
            _settings(database_url=given)

    def test_missing_database_url_raises_configuration_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("DATABASE_URL", raising=False)
        with pytest.raises(ConfigurationError):
            get_settings()


class TestCsvOptions:
    def test_notification_providers_split_on_commas(self) -> None:
        settings = _settings(notify_default_providers="console, email ,TELEGRAM")
        assert settings.notification_providers == ("console", "email", "telegram")

    def test_url_schemes_split_and_lowercased(self) -> None:
        assert _settings(allowed_url_schemes="HTTPS,http").url_schemes == ("https", "http")

    def test_unknown_scheme_rejected(self) -> None:
        with pytest.raises(ValueError, match="unsupported URL scheme"):
            _settings(allowed_url_schemes="https,ftp")

    def test_empty_scheme_list_rejected(self) -> None:
        with pytest.raises(ValueError, match="at least one scheme"):
            _settings(allowed_url_schemes=" , ")


class TestRedaction:
    def test_secrets_are_masked(self) -> None:
        settings = _settings(
            smtp_password="hunter2",
            telegram_bot_token="123:ABC",
            api_key="topsecret",
        )
        dumped = settings.redacted()

        assert dumped["smtp_password"] == "***set***"
        assert dumped["telegram_bot_token"] == "***set***"
        assert dumped["api_key"] == "***set***"
        assert "hunter2" not in str(dumped)
        assert "topsecret" not in str(dumped)

    def test_database_password_is_masked(self) -> None:
        dumped = _settings(database_url="postgresql://user:s3cret@host:5432/db").redacted()
        assert dumped["database_url"] == "postgresql+psycopg://user:***@host:5432/db"
        assert "s3cret" not in str(dumped)

    def test_unset_secret_stays_none(self) -> None:
        assert _settings().redacted()["smtp_password"] is None

    def test_csv_fields_render_as_lists(self) -> None:
        dumped = _settings(notify_default_providers="console,email").redacted()
        assert dumped["notify_default_providers"] == ["console", "email"]


class TestMaskDsnPassword:
    @pytest.mark.parametrize(
        ("given", "expected"),
        [
            ("postgresql://u:p@h:5432/d", "postgresql://u:***@h:5432/d"),
            # No credentials to mask -- returned unchanged.
            ("postgresql://h:5432/d", "postgresql://h:5432/d"),
            ("postgresql://user@h:5432/d", "postgresql://user@h:5432/d"),
            ("not a dsn", "not a dsn"),
        ],
    )
    def test_masks_only_the_password(self, given: str, expected: str) -> None:
        assert mask_dsn_password(given) == expected


class TestBounds:
    def test_check_interval_has_a_floor(self) -> None:
        """Below 60s we would hammer stores; the schema refuses it."""
        with pytest.raises(ValueError):
            _settings(check_interval_seconds=5)  # type: ignore[arg-type]

    def test_defaults_are_conservative(self) -> None:
        settings = _settings()
        assert settings.block_private_addresses is True
        assert settings.api_key is None
        assert settings.notification_providers == ("console",)
