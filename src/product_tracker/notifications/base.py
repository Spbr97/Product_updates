"""The notification provider interface.

A provider turns a :class:`~product_tracker.domain.models.NotificationMessage` into a
delivered message. It knows nothing about products, prices, or why the alert exists -- the
tracking engine decided that already. Adding a channel means writing one class and
registering it.

Contract for implementers:

* ``is_configured`` must not raise and must not perform I/O. It answers "do I have the
  settings I need?" so the registry can skip a provider without attempting delivery.
* ``send`` raises :class:`NotificationDeliveryError` on failure. The service records the
  reason and retries within bounds; it never lets a provider failure abort a check.
* Never put a secret in an exception message -- the text is stored in the database.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import ClassVar

from ..domain.models import NotificationMessage


class NotificationProvider(ABC):
    """Delivers a message through one channel."""

    #: Stable identifier used in settings and in the ``notifications.provider`` column.
    slug: ClassVar[str]
    #: Human-readable name for CLI output.
    display_name: ClassVar[str]

    @abstractmethod
    def is_configured(self) -> bool:
        """Whether this provider has everything it needs. No I/O, never raises."""

    @abstractmethod
    def send(self, message: NotificationMessage) -> None:
        """Deliver the message, or raise ``NotificationDeliveryError``."""

    def __repr__(self) -> str:
        return f"<{type(self).__name__} slug={self.slug!r}>"


def render_plain_text(message: NotificationMessage) -> str:
    """A channel-agnostic plain-text rendering, used by most providers."""
    lines = [message.title, "", message.body]
    if message.url:
        lines.extend(["", message.url])
    return "\n".join(lines)
