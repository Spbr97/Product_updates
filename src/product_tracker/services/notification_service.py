"""Turning rule matches into delivered notifications.

Two separate steps, deliberately:

1. **Record.** A matched rule becomes a ``notifications`` row, guarded by a unique
   ``dedupe_key``. This is the durable decision that an alert is owed.
2. **Deliver.** Rows are handed to providers. A provider failure marks the row ``failed``
   and leaves it to be retried on a later pass; it never aborts a check.

Keeping them apart is what makes delivery idempotent: the row exists before anyone tries
to send it, so a crash between the two leaves a pending row rather than a silent loss.

**Deduplication.** The key is a digest of product, rule, event type, a rule-type-specific
signature (the price transition, or the resulting availability), and a time bucket. So:

* the same alert observed repeatedly within one window is delivered once;
* the same transition in a later window alerts again, because a price that drops every
  Monday is news every Monday;
* the window is ``NOTIFICATION_DEDUPE_WINDOW_SECONDS`` (a day by default) -- shorten it for
  volatile prices -- and a rule's ``cooldown_seconds`` is the per-rule equivalent.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from ..core.config import Settings
from ..core.logging import (
    EVENT_NOTIFICATION_CREATED,
    EVENT_NOTIFICATION_FAILED,
    EVENT_NOTIFICATION_SENT,
    get_logger,
)
from ..db.models import Notification
from ..domain.errors import NotificationDeliveryError
from ..domain.models import NotificationMessage, RuleMatch
from ..notifications.base import NotificationProvider
from ..notifications.registry import active_providers
from ..repositories.notifications import NotificationRepository
from .rules_engine import dedupe_signature

log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class DeliveryReport:
    created: int
    sent: int
    failed: int
    suppressed: int


#: Default suppression window, matching ``Settings.notification_dedupe_window_seconds``.
DEFAULT_DEDUPE_WINDOW_SECONDS = 86_400


def build_dedupe_key(
    match: RuleMatch,
    *,
    when: datetime,
    window_seconds: int = DEFAULT_DEDUPE_WINDOW_SECONDS,
) -> str:
    """A stable digest identifying this alert within one suppression window.

    The window is a bucket over absolute time, not a calendar day: two observations in the
    same bucket share a key and so alert once, and the next bucket alerts again. Bucketing
    rather than "time since last alert" keeps the key a pure function of the observation,
    which is what lets a UNIQUE constraint enforce it without a read-then-write race.

    Hashed rather than concatenated so the column has a bounded length however long a
    signature grows.
    """
    bucket = int(when.astimezone(UTC).timestamp() // max(1, window_seconds))
    parts = "|".join(
        [
            str(match.product_id),
            str(match.rule_id),
            match.rule_type.value,
            dedupe_signature(match),
            str(bucket),
        ]
    )
    return hashlib.sha256(parts.encode("utf-8")).hexdigest()


class NotificationService:
    def __init__(
        self,
        session: Session,
        settings: Settings,
        providers: list[NotificationProvider] | None = None,
    ) -> None:
        self.session = session
        self.settings = settings
        # Injectable so tests can supply a recording provider without patching globals.
        self._providers = providers
        self.repo = NotificationRepository(session)

    @property
    def providers(self) -> list[NotificationProvider]:
        if self._providers is None:
            self._providers = active_providers(self.settings)
        return self._providers

    def record(self, match: RuleMatch, *, when: datetime | None = None) -> Notification | None:
        """Persist the decision to alert. ``None`` when this alert already exists."""
        moment = when or datetime.now(UTC)
        dedupe_key = build_dedupe_key(
            match,
            when=moment,
            window_seconds=self.settings.notification_dedupe_window_seconds,
        )

        notification = self.repo.create_if_new(
            product_id=match.product_id,
            tracking_rule_id=match.rule_id,
            event_type=match.rule_type.value,
            dedupe_key=dedupe_key,
            payload={
                "title": match.title,
                "body": match.body,
                "provider": match.context.get("notify_provider"),
                "context": match.context,
            },
        )

        if notification is None:
            log.info(
                "notification.deduplicated",
                product_id=match.product_id,
                rule_id=match.rule_id,
                event_type=match.rule_type.value,
            )
            return None

        log.info(
            EVENT_NOTIFICATION_CREATED,
            notification_id=notification.id,
            product_id=match.product_id,
            rule_id=match.rule_id,
            event_type=match.rule_type.value,
        )
        return notification

    def deliver(self, notification: Notification) -> bool:
        """Send one notification through the first provider that accepts it.

        Returns True on delivery. Providers are tried in configured order and the first
        success wins -- the intent of a provider list is "reach me", not "tell me four
        times". A channel-specific rule can set ``notify_provider`` instead.
        """
        providers = self._providers_for(notification)

        if not providers:
            self.repo.mark_suppressed(
                notification, reason="no configured notification provider accepted this alert"
            )
            log.warning(
                "notification.no_provider",
                notification_id=notification.id,
                product_id=notification.product_id,
            )
            return False

        message = _message_from(notification)
        errors = []

        for provider in providers:
            try:
                provider.send(message)
            except NotificationDeliveryError as exc:
                errors.append(f"{provider.slug}: {exc.reason}")
                continue
            except Exception as exc:
                # A provider raising something unexpected is a bug in that provider; it
                # must not take down the check that produced the alert.
                errors.append(f"{provider.slug}: unexpected {type(exc).__name__}")
                log.warning(
                    "notification.provider_error", provider=provider.slug, exc_info=exc
                )
                continue

            self.repo.mark_sent(notification, provider=provider.slug, when=datetime.now(UTC))
            log.info(
                EVENT_NOTIFICATION_SENT,
                notification_id=notification.id,
                provider=provider.slug,
                product_id=notification.product_id,
            )
            return True

        reason = "; ".join(errors)
        self.repo.mark_failed(notification, error=reason)
        log.warning(
            EVENT_NOTIFICATION_FAILED,
            notification_id=notification.id,
            product_id=notification.product_id,
            attempts=notification.attempts,
            reason=reason,
        )
        return False

    def record_all(self, matches: list[RuleMatch], *, when: datetime | None = None) -> int:
        """Record a batch of matches without sending anything.

        Returns how many were new. Callers deliver afterwards, in their own transaction.
        """
        return sum(1 for match in matches if self.record(match, when=when) is not None)

    def dispatch(self, matches: list[RuleMatch], *, when: datetime | None = None) -> DeliveryReport:
        """Record and deliver in one go.

        Convenient for tests and one-off scripts. Production paths use ``record_all``
        followed by ``deliver_pending`` so that delivery is outside the check's
        transaction.
        """
        created = sent = failed = suppressed = 0

        for match in matches:
            notification = self.record(match, when=when)
            if notification is None:
                suppressed += 1
                continue
            created += 1
            if self.deliver(notification):
                sent += 1
            else:
                failed += 1

        return DeliveryReport(
            created=created, sent=sent, failed=failed, suppressed=suppressed
        )

    def deliver_pending(
        self, *, limit: int = 50, product_id: int | None = None
    ) -> DeliveryReport:
        """Deliver rows left pending, or failed by an earlier pass and still retryable."""
        rows = self.repo.list_retryable(limit=limit, product_id=product_id)
        sent = failed = 0
        for notification in rows:
            if self.deliver(notification):
                sent += 1
            else:
                failed += 1
        return DeliveryReport(created=0, sent=sent, failed=failed, suppressed=0)

    #: Kept as the name the worker's retry job uses.
    retry_pending = deliver_pending

    def _providers_for(self, notification: Notification) -> list[NotificationProvider]:
        """Honour the rule's provider preference, falling back to all active providers.

        The preference is recorded in the payload when the notification is created, so a
        retry days later still routes the way the rule asked, even if the rule has since
        been edited or deleted.
        """
        preferred = (notification.payload or {}).get("provider")
        if preferred:
            return [p for p in self.providers if p.slug == preferred]
        return self.providers


def _message_from(notification: Notification) -> NotificationMessage:
    payload = notification.payload or {}
    context = payload.get("context") or {}
    return NotificationMessage(
        title=str(payload.get("title") or notification.event_type),
        body=str(payload.get("body") or ""),
        url=context.get("url"),
        context=context,
    )
