"""Email provider (SMTP).

Credentials come from settings and are held as ``SecretStr``; nothing here logs or
re-raises them. Failure messages carry the SMTP error class and code only.
"""

from __future__ import annotations

import smtplib
from email.message import EmailMessage
from typing import ClassVar

from ..core.config import Settings
from ..domain.errors import NotificationDeliveryError
from ..domain.models import NotificationMessage
from .base import NotificationProvider, render_plain_text


class EmailProvider(NotificationProvider):
    slug: ClassVar[str] = "email"
    display_name: ClassVar[str] = "Email (SMTP)"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def is_configured(self) -> bool:
        """Host, sender, and recipient are the minimum; auth is optional (local relays)."""
        return bool(
            self.settings.smtp_host and self.settings.smtp_from and self.settings.smtp_to
        )

    def send(self, message: NotificationMessage) -> None:
        settings = self.settings
        if not self.is_configured():
            raise NotificationDeliveryError(self.slug, "SMTP is not configured")

        email = EmailMessage()
        email["Subject"] = message.title
        email["From"] = str(settings.smtp_from)
        email["To"] = str(settings.smtp_to)
        email.set_content(render_plain_text(message))

        try:
            with smtplib.SMTP(
                str(settings.smtp_host),
                settings.smtp_port,
                timeout=settings.notification_timeout_seconds,
            ) as smtp:
                if settings.smtp_use_tls:
                    smtp.starttls()
                if settings.smtp_username and settings.smtp_password:
                    smtp.login(
                        settings.smtp_username,
                        settings.smtp_password.get_secret_value(),
                    )
                smtp.send_message(email)
        except smtplib.SMTPAuthenticationError as exc:
            # Deliberately does not include the server's reply: some servers echo the
            # username, and this string is persisted.
            raise NotificationDeliveryError(
                self.slug, f"authentication rejected (SMTP {exc.smtp_code})"
            ) from exc
        except smtplib.SMTPException as exc:
            raise NotificationDeliveryError(self.slug, type(exc).__name__) from exc
        except OSError as exc:
            raise NotificationDeliveryError(
                self.slug, f"connection failed: {type(exc).__name__}"
            ) from exc
