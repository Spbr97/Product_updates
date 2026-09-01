"""Console provider -- the default.

Always configured, so a fresh installation alerts somewhere without any setup. Writes to
stderr so that piping ``product-tracker check`` output stays clean.
"""

from __future__ import annotations

import sys
from typing import ClassVar

from ..domain.errors import NotificationDeliveryError
from ..domain.models import NotificationMessage
from .base import NotificationProvider, render_plain_text


class ConsoleProvider(NotificationProvider):
    slug: ClassVar[str] = "console"
    display_name: ClassVar[str] = "Console"

    def is_configured(self) -> bool:
        return True

    def send(self, message: NotificationMessage) -> None:
        try:
            print(f"\n=== ALERT ===\n{render_plain_text(message)}\n", file=sys.stderr, flush=True)
        except OSError as exc:
            # A closed or broken stderr (detached service, closed pipe) is a real
            # delivery failure and should be recorded as one.
            raise NotificationDeliveryError(self.slug, f"cannot write to stderr: {exc}") from exc
