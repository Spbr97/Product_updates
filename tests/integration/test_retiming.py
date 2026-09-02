"""Changing when things happen, after they have been set up.

Both timings were settable only at creation. Re-timing a listing therefore meant removing
and re-adding it, which destroys its price history -- a punishing price for deciding you
wanted hourly checks instead of daily ones.
"""

from __future__ import annotations

from collections.abc import Iterator
from decimal import Decimal

import httpx
import pytest
import respx
from sqlalchemy.orm import Session
from tests.unit.test_adapters import load

from product_tracker.core.config import get_settings
from product_tracker.db.models import PriceHistory
from product_tracker.domain.enums import RuleType
from product_tracker.domain.errors import NotFoundError, ValidationError
from product_tracker.services import user_service
from product_tracker.services.alert_service import AlertService
from product_tracker.services.product_service import ProductService
from product_tracker.stores.registry import default_registry

pytestmark = pytest.mark.db

URL = "https://shop.example.com/p/retime"


@pytest.fixture(autouse=True)
def _respx_router() -> Iterator[None]:
    with respx.mock:
        yield


@pytest.fixture
def owner(db_session: Session) -> int:
    return int(user_service.create_user(db_session, email="owner@example.com").user.id)


def service(session: Session, user_id: int) -> ProductService:
    return ProductService(session, default_registry(), get_settings(), user_id)


def track(session: Session, user_id: int, interval: int | None = None) -> int:
    respx.get(URL).mock(return_value=httpx.Response(200, html=load("jsonld_in_stock.html")))
    product = service(session, user_id).add(URL, check_interval_seconds=interval)
    return int(product.id)


class TestRetimingAListing:
    def test_the_interval_can_be_changed(self, db_session: Session, owner: int) -> None:
        product_id = track(db_session, owner, 3600)

        changed = service(db_session, owner).set_check_interval(product_id, 300)

        assert changed.check_interval_seconds == 300

    def test_history_survives_the_change(self, db_session: Session, owner: int) -> None:
        """The whole point.

        The only way to re-time a listing used to be removing and re-adding it, which took
        every recorded price with it.
        """
        product_id = track(db_session, owner, 3600)
        db_session.add(
            PriceHistory(product_id=product_id, price=Decimal("82900.00"), currency="INR")
        )
        db_session.flush()

        service(db_session, owner).set_check_interval(product_id, 600)

        remaining = (
            db_session.query(PriceHistory).filter(PriceHistory.product_id == product_id).count()
        )
        assert remaining == 1

    def test_clearing_it_returns_to_the_global_default(
        self, db_session: Session, owner: int
    ) -> None:
        product_id = track(db_session, owner, 3600)

        changed = service(db_session, owner).set_check_interval(product_id, None)

        assert changed.check_interval_seconds is None

    def test_below_the_floor_is_refused(self, db_session: Session, owner: int) -> None:
        """Sixty seconds is a politeness floor towards the shops, not a limit on the user."""
        product_id = track(db_session, owner)

        with pytest.raises(ValidationError, match="60s"):
            service(db_session, owner).set_check_interval(product_id, 5)

    def test_someone_elses_listing_cannot_be_retimed(
        self, db_session: Session, owner: int
    ) -> None:
        product_id = track(db_session, owner)
        stranger = int(user_service.create_user(db_session, email="x@example.com").user.id)

        with pytest.raises(NotFoundError):
            service(db_session, stranger).set_check_interval(product_id, 600)

    def test_the_scheduler_reads_the_new_value(
        self, db_session: Session, owner: int
    ) -> None:
        """No restart needed: the worker reconciles from this column every pass."""
        from product_tracker.repositories.products import ProductRepository

        product_id = track(db_session, owner, 3600)
        service(db_session, owner).set_check_interval(product_id, 900)

        scheduled = {
            product.id: product.check_interval_seconds
            for product in ProductRepository(db_session).list_schedulable()
        }
        assert scheduled[product_id] == 900


class TestRetimingAnAlert:
    def test_the_cooldown_can_be_changed(self, db_session: Session, owner: int) -> None:
        product_id = track(db_session, owner)
        alerts = AlertService(db_session, owner)
        rule = alerts.add(product_id, RuleType.PRICE_DROPPED, cooldown_seconds=3600)

        changed = alerts.set_cooldown(rule.id, 86400)

        assert changed.cooldown_seconds == 86400

    def test_clearing_it_removes_the_gap(self, db_session: Session, owner: int) -> None:
        product_id = track(db_session, owner)
        alerts = AlertService(db_session, owner)
        rule = alerts.add(product_id, RuleType.PRICE_DROPPED, cooldown_seconds=3600)

        assert alerts.set_cooldown(rule.id, None).cooldown_seconds is None

    def test_a_negative_cooldown_is_refused(self, db_session: Session, owner: int) -> None:
        product_id = track(db_session, owner)
        alerts = AlertService(db_session, owner)
        rule = alerts.add(product_id, RuleType.PRICE_DROPPED)

        with pytest.raises(ValidationError):
            alerts.set_cooldown(rule.id, -1)

    def test_someone_elses_alert_cannot_be_retimed(
        self, db_session: Session, owner: int
    ) -> None:
        product_id = track(db_session, owner)
        rule = AlertService(db_session, owner).add(product_id, RuleType.PRICE_DROPPED)
        stranger = int(user_service.create_user(db_session, email="y@example.com").user.id)

        with pytest.raises(NotFoundError):
            AlertService(db_session, stranger).set_cooldown(rule.id, 60)
