"""Notification providers.

Each channel implements ``NotificationProvider``; the registry assembles the ones this
installation has configured. The tracking engine never imports a concrete provider.
"""

from .base import NotificationProvider, render_plain_text
from .console import ConsoleProvider
from .email import EmailProvider
from .registry import ALL_PROVIDERS, active_providers, build_provider, provider_status
from .telegram import TelegramProvider
from .webhook import WebhookProvider

__all__ = [
    "ALL_PROVIDERS",
    "ConsoleProvider",
    "EmailProvider",
    "NotificationProvider",
    "TelegramProvider",
    "WebhookProvider",
    "active_providers",
    "build_provider",
    "provider_status",
    "render_plain_text",
]
