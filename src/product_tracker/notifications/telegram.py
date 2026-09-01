"""Telegram provider.

The bot token is part of the API *URL*, so no error message from this module may include
the URL -- only the status code and a truncated response body.
"""

from __future__ import annotations

from typing import ClassVar

import httpx

from ..core.config import Settings
from ..domain.errors import NotificationDeliveryError
from ..domain.models import NotificationMessage
from .base import NotificationProvider, render_plain_text

API_BASE = "https://api.telegram.org"


class TelegramProvider(NotificationProvider):
    slug: ClassVar[str] = "telegram"
    display_name: ClassVar[str] = "Telegram"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def is_configured(self) -> bool:
        return bool(self.settings.telegram_bot_token and self.settings.telegram_chat_id)

    def send(self, message: NotificationMessage) -> None:
        settings = self.settings
        if not self.is_configured() or settings.telegram_bot_token is None:
            raise NotificationDeliveryError(self.slug, "Telegram is not configured")

        token = settings.telegram_bot_token.get_secret_value()
        url = f"{API_BASE}/bot{token}/sendMessage"

        try:
            response = httpx.post(
                url,
                json={
                    "chat_id": settings.telegram_chat_id,
                    "text": render_plain_text(message),
                    "disable_web_page_preview": True,
                },
                timeout=settings.notification_timeout_seconds,
            )
        except httpx.HTTPError as exc:
            # str(exc) can embed the request URL, which carries the bot token.
            raise NotificationDeliveryError(self.slug, type(exc).__name__) from exc

        if response.status_code >= 400:
            raise NotificationDeliveryError(
                self.slug,
                f"HTTP {response.status_code}: {response.text[:200]}",
            )
