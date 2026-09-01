"""Notification provider registry.

Assembles the providers this installation should actually use: those named in
``NOTIFY_DEFAULT_PROVIDERS`` that also report themselves configured. A provider named but
missing its settings is skipped with a warning rather than failing every alert -- one
mis-set SMTP password should not stop the console provider working.
"""

from __future__ import annotations

from ..core.config import Settings
from ..core.logging import get_logger
from .base import NotificationProvider
from .console import ConsoleProvider
from .email import EmailProvider
from .telegram import TelegramProvider
from .webhook import WebhookProvider

log = get_logger(__name__)

#: Every provider compiled into this build. Add new channels here.
ALL_PROVIDERS: dict[str, type[NotificationProvider]] = {
    ConsoleProvider.slug: ConsoleProvider,
    EmailProvider.slug: EmailProvider,
    TelegramProvider.slug: TelegramProvider,
    WebhookProvider.slug: WebhookProvider,
}


def build_provider(slug: str, settings: Settings) -> NotificationProvider | None:
    """Instantiate one provider by slug, or ``None`` if the slug is unknown."""
    provider_class = ALL_PROVIDERS.get(slug)
    if provider_class is None:
        return None
    # ConsoleProvider needs no settings; the rest take them.
    if provider_class is ConsoleProvider:
        return ConsoleProvider()
    return provider_class(settings)  # type: ignore[call-arg]


def active_providers(settings: Settings) -> list[NotificationProvider]:
    """The configured providers named in settings, in the order they were named."""
    providers = []
    for slug in settings.notification_providers:
        provider = build_provider(slug, settings)
        if provider is None:
            log.warning("notification.unknown_provider", provider=slug)
            continue
        if not provider.is_configured():
            log.warning("notification.provider_unconfigured", provider=slug)
            continue
        providers.append(provider)
    return providers


def provider_status(settings: Settings) -> list[tuple[str, str, bool, bool]]:
    """``(slug, display_name, enabled, configured)`` for every known provider.

    Used by ``product-tracker status`` and the readiness probe, so an operator can see at
    a glance which channels would actually deliver.
    """
    enabled = set(settings.notification_providers)
    rows = []
    for slug, provider_class in ALL_PROVIDERS.items():
        provider = build_provider(slug, settings)
        rows.append(
            (
                slug,
                provider_class.display_name,
                slug in enabled,
                provider.is_configured() if provider else False,
            )
        )
    return rows
