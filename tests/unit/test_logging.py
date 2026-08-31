"""Log redaction.

Secrets must not reach the log stream even if a caller passes one by accident, so the
redaction processor is tested directly rather than trusted.
"""

from __future__ import annotations

import pytest

from product_tracker.core.logging import _redact_secrets, configure_logging


def redact(**event: object) -> dict:
    return _redact_secrets(None, "info", dict(event))


class TestRedaction:
    @pytest.mark.parametrize(
        "key",
        [
            "password",
            "passwd",
            "smtp_password",
            "secret",
            "client_secret",
            "token",
            "telegram_bot_token",
            "api_key",
            "apikey",
            "api-key",
            "authorization",
            "cookie",
            "credential",
            "AWS_SECRET_ACCESS_KEY",
        ],
    )
    def test_sensitive_keys_are_masked(self, key: str) -> None:
        assert redact(**{key: "leaked-value"})[key] == "***"

    @pytest.mark.parametrize("key", ["product_id", "store", "price", "url", "status"])
    def test_ordinary_keys_survive(self, key: str) -> None:
        assert redact(**{key: "visible"})[key] == "visible"

    def test_nested_mappings_are_redacted(self) -> None:
        event = redact(context={"user": "siba", "api_key": "leaked", "nested": {"token": "x"}})
        assert event["context"]["user"] == "siba"
        assert event["context"]["api_key"] == "***"
        assert event["context"]["nested"]["token"] == "***"

    def test_no_secret_value_survives_anywhere(self) -> None:
        event = redact(
            event="notification.sent",
            provider="telegram",
            telegram_bot_token="123456:REAL",
            payload={"authorization": "Bearer REAL"},
        )
        assert "REAL" not in str(event)
        assert event["provider"] == "telegram"


class TestConfigureLogging:
    @pytest.mark.parametrize("fmt", ["json", "console"])
    def test_configures_without_error_and_is_idempotent(self, fmt: str) -> None:
        configure_logging(level="DEBUG", fmt=fmt)
        configure_logging(level="INFO", fmt=fmt)

    def test_emits_a_line(self, capsys: pytest.CaptureFixture[str]) -> None:
        from product_tracker.core.logging import get_logger

        configure_logging(level="INFO", fmt="json")
        get_logger("test").info("check.started", product_id=1, api_key="leaked")

        captured = capsys.readouterr().err
        assert "check.started" in captured
        assert "leaked" not in captured
