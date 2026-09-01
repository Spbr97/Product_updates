"""Notification providers and the registry.

The recurring concern: a provider's error text is stored in the database, so no failure
message may carry a token, password, or webhook URL.
"""

from __future__ import annotations

import smtplib
from collections.abc import Iterator

import httpx
import pytest
import respx

from product_tracker.core.config import Settings
from product_tracker.domain.errors import NotificationDeliveryError
from product_tracker.domain.models import NotificationMessage
from product_tracker.notifications.base import render_plain_text
from product_tracker.notifications.console import ConsoleProvider
from product_tracker.notifications.email import EmailProvider
from product_tracker.notifications.registry import (
    ALL_PROVIDERS,
    active_providers,
    build_provider,
    provider_status,
)
from product_tracker.notifications.telegram import TelegramProvider
from product_tracker.notifications.webhook import WebhookProvider

BOT_TOKEN = "123456:SUPERSECRETTOKEN"
HOOK_URL = "https://hooks.example.com/services/SECRETHOOKPATH"

MESSAGE = NotificationMessage(
    title="Price drop: Test Product",
    body="₹100.00 -> ₹90.00 (-10.0%)",
    url="https://shop.example.com/p/1",
    context={"current_price": "90"},
)


@pytest.fixture(autouse=True)
def _respx_router() -> Iterator[None]:
    """Activate respx for every test in this module.

    Not ``@respx.mock`` on the class: in respx 0.23 that decorator returns a *function*,
    so pytest silently stops collecting the class and the tests never run.
    """
    with respx.mock:
        yield


def settings(**overrides: object) -> Settings:
    base = {"database_url": "postgresql://u:p@localhost:5432/db"}
    return Settings(**{**base, **overrides})  # type: ignore[arg-type]


class TestRendering:
    def test_includes_title_body_and_url(self) -> None:
        text = render_plain_text(MESSAGE)

        assert "Price drop: Test Product" in text
        assert "-10.0%" in text
        assert "https://shop.example.com/p/1" in text

    def test_omits_a_missing_url(self) -> None:
        text = render_plain_text(NotificationMessage(title="T", body="B"))
        assert text.strip().endswith("B")


class TestConsoleProvider:
    def test_is_always_configured(self) -> None:
        """The default channel must work on a fresh install with no setup."""
        assert ConsoleProvider().is_configured()

    def test_writes_to_stderr(self, capsys: pytest.CaptureFixture[str]) -> None:
        ConsoleProvider().send(MESSAGE)

        captured = capsys.readouterr()
        assert "Price drop: Test Product" in captured.err
        assert captured.out == ""


class TestEmailProvider:
    def test_unconfigured_without_host(self) -> None:
        assert not EmailProvider(settings()).is_configured()

    def test_configured_with_host_from_and_to(self) -> None:
        provider = EmailProvider(
            settings(smtp_host="smtp.example.com", smtp_from="a@x.com", smtp_to="b@x.com")
        )
        assert provider.is_configured()

    def test_send_without_configuration_raises(self) -> None:
        with pytest.raises(NotificationDeliveryError, match="not configured"):
            EmailProvider(settings()).send(MESSAGE)

    def test_connection_failure_is_a_delivery_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        provider = EmailProvider(
            settings(smtp_host="smtp.example.com", smtp_from="a@x.com", smtp_to="b@x.com")
        )

        def refuse(*_args: object, **_kwargs: object) -> None:
            raise OSError("connection refused")

        monkeypatch.setattr(smtplib, "SMTP", refuse)

        with pytest.raises(NotificationDeliveryError) as excinfo:
            provider.send(MESSAGE)
        assert "connection failed" in str(excinfo.value)

    def test_auth_failure_does_not_leak_the_password(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        provider = EmailProvider(
            settings(
                smtp_host="smtp.example.com",
                smtp_from="a@x.com",
                smtp_to="b@x.com",
                smtp_username="user",
                smtp_password="hunter2",
            )
        )

        class FakeSMTP:
            def __init__(self, *_a: object, **_k: object) -> None: ...
            def __enter__(self) -> FakeSMTP:
                return self

            def __exit__(self, *_a: object) -> None: ...
            def starttls(self) -> None: ...
            def login(self, *_a: object) -> None:
                raise smtplib.SMTPAuthenticationError(535, b"5.7.8 bad credentials for user")

        monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)

        with pytest.raises(NotificationDeliveryError) as excinfo:
            provider.send(MESSAGE)

        message = str(excinfo.value)
        assert "hunter2" not in message
        assert "535" in message


class TestTelegramProvider:
    def _provider(self) -> TelegramProvider:
        return TelegramProvider(
            settings(telegram_bot_token=BOT_TOKEN, telegram_chat_id="42")
        )

    def test_unconfigured_without_token(self) -> None:
        assert not TelegramProvider(settings(telegram_chat_id="42")).is_configured()

    def test_unconfigured_without_chat_id(self) -> None:
        assert not TelegramProvider(settings(telegram_bot_token=BOT_TOKEN)).is_configured()

    def test_posts_the_message(self) -> None:
        route = respx.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage").mock(
            return_value=httpx.Response(200, json={"ok": True})
        )

        self._provider().send(MESSAGE)

        assert route.called
        body = route.calls[0].request.content.decode()
        assert "Price drop" in body
        assert '"chat_id":"42"' in body

    def test_http_error_is_a_delivery_error(self) -> None:
        respx.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage").mock(
            return_value=httpx.Response(400, text="Bad Request: chat not found")
        )

        with pytest.raises(NotificationDeliveryError, match="chat not found"):
            self._provider().send(MESSAGE)

    def test_transport_error_does_not_leak_the_token(self) -> None:
        """httpx puts the request URL in its message, and the URL contains the token."""
        respx.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage").mock(
            side_effect=httpx.ConnectError(
                f"failed connecting to https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
            )
        )

        with pytest.raises(NotificationDeliveryError) as excinfo:
            self._provider().send(MESSAGE)

        assert "SUPERSECRETTOKEN" not in str(excinfo.value)


class TestWebhookProvider:
    def test_unconfigured_without_url(self) -> None:
        assert not WebhookProvider(settings()).is_configured()

    def test_posts_a_slack_compatible_body(self) -> None:
        route = respx.post(HOOK_URL).mock(return_value=httpx.Response(200))

        WebhookProvider(settings(webhook_url=HOOK_URL)).send(MESSAGE)

        payload = route.calls[0].request.content.decode()
        assert '"text"' in payload  # Slack and Discord both read `text`.
        assert "Price drop" in payload

    def test_error_status_is_a_delivery_error(self) -> None:
        respx.post(HOOK_URL).mock(return_value=httpx.Response(404, text="no_service"))

        with pytest.raises(NotificationDeliveryError, match="no_service"):
            WebhookProvider(settings(webhook_url=HOOK_URL)).send(MESSAGE)

    def test_transport_error_does_not_leak_the_hook_url(self) -> None:
        """Anyone holding the webhook URL can post to the channel; it is a secret."""
        respx.post(HOOK_URL).mock(
            side_effect=httpx.ConnectError(f"failed connecting to {HOOK_URL}")
        )

        with pytest.raises(NotificationDeliveryError) as excinfo:
            WebhookProvider(settings(webhook_url=HOOK_URL)).send(MESSAGE)

        assert "SECRETHOOKPATH" not in str(excinfo.value)


class TestRegistry:
    def test_knows_every_provider(self) -> None:
        assert set(ALL_PROVIDERS) == {"console", "email", "telegram", "webhook"}

    def test_unknown_slug_returns_none(self) -> None:
        assert build_provider("carrier-pigeon", settings()) is None

    def test_console_is_active_by_default(self) -> None:
        providers = active_providers(settings())
        assert [p.slug for p in providers] == ["console"]

    def test_unconfigured_providers_are_skipped(self) -> None:
        """A missing SMTP password must not stop the console provider working."""
        providers = active_providers(settings(notify_default_providers="email,console"))
        assert [p.slug for p in providers] == ["console"]

    def test_unknown_provider_names_are_skipped(self) -> None:
        providers = active_providers(settings(notify_default_providers="nope,console"))
        assert [p.slug for p in providers] == ["console"]

    def test_order_is_preserved(self) -> None:
        providers = active_providers(
            settings(notify_default_providers="webhook,console", webhook_url=HOOK_URL)
        )
        assert [p.slug for p in providers] == ["webhook", "console"]

    def test_status_reports_enabled_and_configured_separately(self) -> None:
        rows = {slug: (enabled, configured) for slug, _, enabled, configured in
                provider_status(settings(notify_default_providers="email"))}

        assert rows["email"] == (True, False)  # asked for, but not usable
        assert rows["console"] == (False, True)  # usable, but not asked for
