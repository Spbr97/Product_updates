"""Each person's alerts reach that person, and nobody else.

Rules have been per-account since migration 0007. Delivery was not: ``SMTP_TO`` and
``TELEGRAM_CHAT_ID`` are deployment-wide, so two people watching two different products
had their alerts arrive in one inbox. Correct for the single-user install this began as,
and wrong the moment a second person has a watchlist.

The test that matters most here is the digest one. Batching was keyed on providers alone,
which was right while every alert went to the same address -- and would, with per-account
destinations, put Alice's alerts and Bob's into one message and send it to whichever of
them the batch happened to name. A delivery-time convenience must never widen who can see
an alert.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from product_tracker.core.config import Settings, get_settings
from product_tracker.db.models import Notification, Product, TrackingRule
from product_tracker.domain.enums import RuleType
from product_tracker.domain.models import NotificationMessage
from product_tracker.notifications.base import NotificationProvider
from product_tracker.services import user_service
from product_tracker.services.notification_service import NotificationService, recipients_for

pytestmark = pytest.mark.db


class Recorder(NotificationProvider):
    """Stands in for a channel, and remembers where it was told to send."""

    slug = "email"
    display_name = "Recorder"

    def __init__(self) -> None:
        self.sent: list[NotificationMessage] = []

    def is_configured(self) -> bool:
        return True

    def send(self, message: NotificationMessage) -> None:
        self.sent.append(message)

    @property
    def destinations(self) -> list[str | None]:
        return [m.recipients.get(self.slug) for m in self.sent]


@pytest.fixture
def settings() -> Settings:
    return get_settings()


@pytest.fixture
def product(db_session: Session) -> Product:
    store = db_session.execute(text("SELECT id FROM stores LIMIT 1")).scalar_one()
    url = "https://shop.example.com/p/notify"
    db_session.execute(text("DELETE FROM products WHERE url_canonical = :u"), {"u": url})
    row = Product(url=url, url_canonical=url, store_id=store, name="Watched")
    db_session.add(row)
    db_session.flush()
    return row


def account(session: Session, email: str, *, notify: str | None) -> int:
    user_id = int(user_service.create_user(session, email=email).user.id)
    if notify is not None:
        user_service.set_notify(session, user_id, email=notify)
    return user_id


def alert_for(session: Session, product: Product, user_id: int, *, key: str) -> Notification:
    """A rule owned by one account, and a notification raised by it."""
    rule = TrackingRule(
        product_id=product.id, user_id=user_id, rule_type=RuleType.PRICE_DROPPED, params={}
    )
    session.add(rule)
    session.flush()

    notification = Notification(
        product_id=product.id,
        tracking_rule_id=rule.id,
        event_type=RuleType.PRICE_DROPPED.value,
        dedupe_key=key,
        payload={"title": f"Price dropped ({key})", "body": "cheaper", "context": {}},
        created_at=datetime.now(UTC) - timedelta(hours=2),
    )
    session.add(notification)
    session.flush()
    return notification


class TestWhereAnAlertGoes:
    def test_it_goes_to_the_account_that_set_the_rule(
        self, db_session: Session, settings: Settings, product: Product
    ) -> None:
        alice = account(db_session, "alice-notify@example.com", notify="alice@inbox.test")
        notification = alert_for(db_session, product, alice, key="alice-1")
        recorder = Recorder()

        NotificationService(db_session, settings, [recorder]).deliver(notification)

        assert recorder.destinations == ["alice@inbox.test"]

    def test_an_account_without_one_falls_back_to_the_deployment(
        self, db_session: Session, settings: Settings, product: Product
    ) -> None:
        """A single-user install sets SMTP_TO and nothing else, and must keep working."""
        plain = account(db_session, "plain-notify@example.com", notify=None)
        notification = alert_for(db_session, product, plain, key="plain-1")
        recorder = Recorder()

        NotificationService(db_session, settings, [recorder]).deliver(notification)

        assert recorder.destinations == [None], "should defer to the deployment setting"

    def test_two_accounts_are_told_apart(
        self, db_session: Session, settings: Settings, product: Product
    ) -> None:
        """The same product, two watchers, two different inboxes."""
        alice = account(db_session, "a-notify@example.com", notify="alice@inbox.test")
        bob = account(db_session, "b-notify@example.com", notify="bob@inbox.test")
        first = alert_for(db_session, product, alice, key="a-2")
        second = alert_for(db_session, product, bob, key="b-2")
        recorder = Recorder()

        service = NotificationService(db_session, settings, [recorder])
        service.deliver(first)
        service.deliver(second)

        assert recorder.destinations == ["alice@inbox.test", "bob@inbox.test"]

    def test_clearing_it_returns_to_the_default(
        self, db_session: Session, settings: Settings, product: Product
    ) -> None:
        someone = account(db_session, "clear-notify@example.com", notify="temp@inbox.test")
        user_service.set_notify(db_session, someone, email="")
        notification = alert_for(db_session, product, someone, key="clear-1")
        recorder = Recorder()

        NotificationService(db_session, settings, [recorder]).deliver(notification)

        assert recorder.destinations == [None]


class TestADigestNeverSpansAccounts:
    """The leak this change could have introduced, pinned so it cannot come back."""

    def test_each_account_gets_its_own_digest(
        self, db_session: Session, settings: Settings, product: Product, monkeypatch
    ) -> None:  # type: ignore[no-untyped-def]
        monkeypatch.setattr(settings, "notification_digest_minutes", 1)
        alice = account(db_session, "ad-notify@example.com", notify="alice@inbox.test")
        bob = account(db_session, "bd-notify@example.com", notify="bob@inbox.test")
        for n in range(2):
            alert_for(db_session, product, alice, key=f"ad-{n}")
            alert_for(db_session, product, bob, key=f"bd-{n}")
        recorder = Recorder()

        NotificationService(db_session, settings, [recorder]).deliver_pending()

        assert sorted(d or "" for d in recorder.destinations) == [
            "alice@inbox.test",
            "bob@inbox.test",
        ], "one digest each, not one shared"

    def test_no_digest_carries_another_account_s_alert(
        self, db_session: Session, settings: Settings, product: Product, monkeypatch
    ) -> None:  # type: ignore[no-untyped-def]
        """The stronger form: not just two messages, but the right lines in each."""
        monkeypatch.setattr(settings, "notification_digest_minutes", 1)
        alice = account(db_session, "ax-notify@example.com", notify="alice@inbox.test")
        bob = account(db_session, "bx-notify@example.com", notify="bob@inbox.test")
        for n in range(2):
            alert_for(db_session, product, alice, key=f"ax-{n}")
            alert_for(db_session, product, bob, key=f"bx-{n}")
        recorder = Recorder()

        NotificationService(db_session, settings, [recorder]).deliver_pending()

        for message in recorder.sent:
            destination = message.recipients.get("email")
            other = "bx-" if destination == "alice@inbox.test" else "ax-"
            assert other not in message.body, f"{destination} was sent someone else's alerts"


class TestResolution:
    def test_an_alert_with_no_rule_resolves_to_nothing(
        self, db_session: Session, product: Product
    ) -> None:
        """A deleted rule leaves its notification behind, and delivery must not raise."""
        orphan = Notification(
            product_id=product.id,
            tracking_rule_id=None,
            event_type="price_dropped",
            dedupe_key="orphan-1",
            payload={"title": "t", "body": "b", "context": {}},
        )
        db_session.add(orphan)
        db_session.flush()

        assert recipients_for(orphan) == {}


class TestTheCliSetsIt:
    def test_only_what_is_passed_changes(self, db_session: Session) -> None:
        """Setting an email must not silently clear a Telegram id set last week."""
        who = account(db_session, "both-notify@example.com", notify=None)
        user_service.set_notify(db_session, who, telegram_chat_id="12345")
        user_service.set_notify(db_session, who, email="both@inbox.test")

        user = user_service.get_user(db_session, who)
        assert user.notify_email == "both@inbox.test"
        assert user.notify_telegram_chat_id == "12345"


class TestPriceMovesBothWays:
    """What the whole feature is for: an increase and a decrease both reach their owner."""

    @pytest.mark.parametrize(
        ("rule_type", "label"),
        [(RuleType.PRICE_DROPPED, "dropped"), (RuleType.PRICE_INCREASED, "increased")],
    )
    def test_either_direction_reaches_the_owner(
        self,
        db_session: Session,
        settings: Settings,
        product: Product,
        rule_type: RuleType,
        label: str,
    ) -> None:
        owner = account(db_session, f"{label}-notify@example.com", notify=f"{label}@inbox.test")
        rule = TrackingRule(
            product_id=product.id, user_id=owner, rule_type=rule_type, params={}
        )
        db_session.add(rule)
        db_session.flush()
        notification = Notification(
            product_id=product.id,
            tracking_rule_id=rule.id,
            event_type=rule_type.value,
            dedupe_key=f"{label}-key",
            payload={
                "title": f"Price {label}",
                "body": f"now {Decimal('61330')}",
                "context": {},
            },
        )
        db_session.add(notification)
        db_session.flush()
        recorder = Recorder()

        NotificationService(db_session, settings, [recorder]).deliver(notification)

        assert recorder.destinations == [f"{label}@inbox.test"]
