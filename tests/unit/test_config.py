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


class TestDeliveryPincode:
    def test_unset_by_default(self) -> None:
        """Nobody gets a delivery area they did not ask for."""
        assert _settings().delivery_pincode is None

    @pytest.mark.parametrize("given", ["", "   "])
    def test_blank_is_unset(self, given: str) -> None:
        """``DELIVERY_PINCODE=`` in a .env file arrives as an empty string."""
        assert _settings(delivery_pincode=given).delivery_pincode is None

    def test_a_valid_pincode_is_kept(self) -> None:
        assert _settings(delivery_pincode=" 560037 ").delivery_pincode == "560037"

    @pytest.mark.parametrize("given", ["56003", "5600377", "56003a", "560 037", "abcdef"])
    def test_a_malformed_pincode_is_refused(self, given: str) -> None:
        """Loudly, at startup. Sent silently to every retailer and quietly ignored, a
        typo'd pincode leaves prices from the shop's default area while the operator
        believes they are local."""
        with pytest.raises(ValueError):
            _settings(delivery_pincode=given)

    def test_it_is_not_a_secret(self) -> None:
        """A PIN code is not a credential, so ``product-tracker config`` shows it --
        which is the point: an operator must be able to see what they configured."""
        assert _settings(delivery_pincode="560037").redacted()["delivery_pincode"] == "560037"


class TestNotificationDigest:
    def test_off_by_default(self) -> None:
        """An install that never asked for batching must not start getting it."""
        assert _settings().notification_digest_minutes == 0

    def test_a_window_is_accepted(self) -> None:
        assert _settings(notification_digest_minutes=30).notification_digest_minutes == 30

    @pytest.mark.parametrize("given", [-1, 1441])
    def test_out_of_range_is_refused(self, given: int) -> None:
        """Negative is meaningless; beyond a day an "alert" is no longer an alert."""
        with pytest.raises(ValueError):
            _settings(notification_digest_minutes=given)  # type: ignore[arg-type]


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
