"""Batching alerts into one message instead of many.

Thirty tracked products having a quiet Tuesday should not be thirty separate
interruptions. But a digest is a *delivery-time* grouping only: every alert keeps its own
row, its own dedupe key, and its exactly-once guarantee, because the moment batching
starts merging rows it becomes a way to lose an alert.

So most of what is tested here is what the digest must NOT change.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session
from tests.integration.test_alerts import RecordingProvider

from product_tracker.core.config import Settings, get_settings
from product_tracker.db.models import Notification, Product, Store
from product_tracker.domain.enums import NotificationStatus, RuleType
from product_tracker.domain.models import RuleMatch
from product_tracker.services.notification_service import NotificationService

pytestmark = pytest.mark.db


def settings_with(minutes: int) -> Settings:
    return get_settings().model_copy(update={"notification_digest_minutes": minutes})


@pytest.fixture
def provider() -> RecordingProvider:
    """Captures deliveries instead of sending them. Imported rather than redefined so
    the digest is exercised through the same provider the rest of the suite uses."""
    return RecordingProvider()


@pytest.fixture
def products(db_session: Session) -> list[int]:
    """Three tracked listings, so a batch has something to batch."""
    store = db_session.execute(select(Store).limit(1)).scalar_one()
    ids = []
    for i in range(3):
        product = Product(
            url=f"https://shop.example.com/p/digest-{i}",
            url_canonical=f"https://shop.example.com/p/digest-{i}",
            store_id=store.id,
            name=f"Digest Product {i}",
        )
        db_session.add(product)
        db_session.flush()
        ids.append(product.id)
    return ids


def match(product_id: int, n: int) -> RuleMatch:
    return RuleMatch(
        product_id=product_id,
        rule_id=None,
        rule_type=RuleType.PRICE_DROPPED,
        title=f"Digest Product {n} dropped to Rs 100",
        body=f"was Rs 200, now Rs 100 at shop {n}",
        context={"url": f"https://shop.example.com/p/digest-{n}"},
    )


def record_all(
    session: Session, provider: RecordingProvider, product_ids: list[int], *, minutes: int
) -> NotificationService:
    service = NotificationService(session, settings_with(minutes), providers=[provider])
    for n, pid in enumerate(product_ids):
        service.record(match(pid, n))
    session.flush()
    return service


def age_rows(session: Session, minutes: int) -> None:
    """Backdate every pending row, standing in for the window having elapsed."""
    when = datetime.now(UTC) - timedelta(minutes=minutes)
    for row in session.execute(select(Notification)).scalars():
        row.created_at = when
    session.flush()


class TestDigestOffIsUnchanged:
    def test_each_alert_is_its_own_message(
        self, db_session: Session, provider: RecordingProvider, products: list[int]
    ) -> None:
        """The default. Every install that has not asked for a digest must not get one."""
        service = record_all(db_session, provider, products, minutes=0)

        report = service.deliver_pending()

        assert report.sent == 3
        assert len(provider.sent) == 3
        assert all("alerts" not in m.title for m in provider.sent)


class TestDigestHoldsThenSendsOnce:
    def test_nothing_is_sent_while_the_window_is_open(
        self, db_session: Session, provider: RecordingProvider, products: list[int]
    ) -> None:
        service = record_all(db_session, provider, products, minutes=30)

        report = service.deliver_pending()

        assert provider.sent == []
        assert report.sent == 0
        # Held, not failed: nothing went wrong, there is simply nothing to send yet.
        assert report.failed == 0

    def test_holding_does_not_burn_a_delivery_attempt(
        self, db_session: Session, provider: RecordingProvider, products: list[int]
    ) -> None:
        """Otherwise a long window would exhaust the retry budget before sending once."""
        service = record_all(db_session, provider, products, minutes=30)

        for _ in range(5):
            service.deliver_pending()

        rows = db_session.execute(select(Notification)).scalars().all()
        assert all(row.attempts == 0 for row in rows)
        assert all(row.status is NotificationStatus.PENDING for row in rows)

    def test_one_message_covers_the_whole_batch(
        self, db_session: Session, provider: RecordingProvider, products: list[int]
    ) -> None:
        service = record_all(db_session, provider, products, minutes=30)
        age_rows(db_session, 31)

        report = service.deliver_pending()

        assert len(provider.sent) == 1
        assert report.sent == 3
        assert provider.sent[0].title == "Product Tracker: 3 alerts"

    def test_the_summary_says_what_happened_not_just_how_much(
        self, db_session: Session, provider: RecordingProvider, products: list[int]
    ) -> None:
        """"5 alerts" that a reader must open the app to understand is a worse
        notification than five separate ones, not a better one."""
        service = record_all(db_session, provider, products, minutes=30)
        age_rows(db_session, 31)

        service.deliver_pending()

        body = provider.sent[0].body
        for n in range(3):
            assert f"Digest Product {n} dropped" in body
            assert f"shop {n}" in body

    def test_every_row_is_marked_sent_exactly_once(
        self, db_session: Session, provider: RecordingProvider, products: list[int]
    ) -> None:
        """The guarantee the digest must not weaken."""
        service = record_all(db_session, provider, products, minutes=30)
        age_rows(db_session, 31)

        service.deliver_pending()
        service.deliver_pending()  # a second pass must find nothing left

        rows = db_session.execute(select(Notification)).scalars().all()
        assert len(rows) == 3
        assert all(row.status is NotificationStatus.SENT for row in rows)
        assert all(row.attempts == 1 for row in rows)
        assert len(provider.sent) == 1

    def test_a_lone_alert_is_sent_as_itself(
        self, db_session: Session, provider: RecordingProvider, products: list[int]
    ) -> None:
        """One alert is not a digest. Titling it "1 alerts" loses the product's name."""
        service = record_all(db_session, provider, products[:1], minutes=30)
        age_rows(db_session, 31)

        service.deliver_pending()

        assert len(provider.sent) == 1
        assert provider.sent[0].title == "Digest Product 0 dropped to Rs 100"


class TestDigestFailure:
    def test_a_failed_digest_leaves_every_row_retryable(
        self, db_session: Session, products: list[int]
    ) -> None:
        """A batch must not turn one provider outage into three lost alerts."""
        failing = RecordingProvider(fail=True)
        service = record_all(db_session, failing, products, minutes=30)
        age_rows(db_session, 31)

        report = service.deliver_pending()

        assert report.failed == 3
        rows = db_session.execute(select(Notification)).scalars().all()
        assert all(row.status is NotificationStatus.FAILED for row in rows)
        assert all(row.error and "provider is down" in row.error for row in rows)

    def test_turning_the_digest_off_sends_the_waiting_rows_individually(
        self, db_session: Session, provider: RecordingProvider, products: list[int]
    ) -> None:
        """Nothing is stranded by a settings change: the rows were never merged."""
        record_all(db_session, provider, products, minutes=30).deliver_pending()
        assert provider.sent == []

        NotificationService(
            db_session, settings_with(0), providers=[provider]
        ).deliver_pending()

        assert len(provider.sent) == 3
