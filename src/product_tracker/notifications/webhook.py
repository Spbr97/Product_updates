"""Generic webhook provider.

Posts a JSON body containing both a rendered ``text`` field and the structured context.
Slack and Discord incoming webhooks both accept a payload with ``text``, so this one
provider covers them without a dedicated class each.

The webhook URL itself is a secret (anyone holding it can post), so it never appears in an
error message.
"""

from __future__ import annotations

from typing import ClassVar

import httpx

from ..core.config import Settings
from ..domain.errors import NotificationDeliveryError
from ..domain.models import NotificationMessage
from .base import NotificationProvider, render_plain_text


class WebhookProvider(NotificationProvider):
    slug: ClassVar[str] = "webhook"
    display_name: ClassVar[str] = "Webhook (Slack/Discord compatible)"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def is_configured(self) -> bool:
        return bool(self.settings.webhook_url)

    def send(self, message: NotificationMessage) -> None:
        settings = self.settings
        if not settings.webhook_url:
            raise NotificationDeliveryError(self.slug, "no webhook URL configured")

        payload = {
            "text": render_plain_text(message),
            "title": message.title,
            "body": message.body,
            "url": message.url,
            "context": message.context,
        }

        try:
            response = httpx.post(
                settings.webhook_url,
                json=payload,
                timeout=settings.notification_timeout_seconds,
            )
        except httpx.HTTPError as exc:
            raise NotificationDeliveryError(self.slug, type(exc).__name__) from exc

        if response.status_code >= 400:
            raise NotificationDeliveryError(
                self.slug, f"HTTP {response.status_code}: {response.text[:200]}"
            )
