"""Per-account ceilings, and the one thing ``is_admin`` actually means.

Quotas exist because one account scheduling unlimited requests can get a shared address
blocked by a retailer -- which costs every other user, not just them. They are a courtesy
ceiling rather than a security boundary: what actually protects a shop is the shared pacing
in ``scheduler/throttle.py``, which no account is exempt from.
"""

from __future__ import annotations

from collections.abc import Iterator

import httpx
import pytest
import respx
from sqlalchemy.orm import Session
from tests.unit.test_adapters import load

from product_tracker.core.config import Settings, get_settings
from product_tracker.domain.enums import RuleType
from product_tracker.domain.errors import QuotaExceededError
from product_tracker.services import group_service, user_service
from product_tracker.services.alert_service import AlertService
from product_tracker.services.product_service import ProductService
from product_tracker.stores.registry import default_registry

pytestmark = pytest.mark.db


@pytest.fixture(autouse=True)
def _respx_router() -> Iterator[None]:
    with respx.mock:
        yield


@pytest.fixture
def member(db_session: Session) -> int:
    return int(user_service.create_user(db_session, email="member@example.com").user.id)


@pytest.fixture
def admin(db_session: Session) -> int:
    created = user_service.create_user(
        db_session, email="operator@example.com", is_admin=True
    )
    return int(created.user.id)


def capped(**overrides: int) -> Settings:
    """Settings with deliberately tiny ceilings, so a test need not create hundreds."""
    base = get_settings().model_dump()
    base.update(overrides)
    return Settings(**base)


def service(session: Session, user_id: int, settings: Settings) -> ProductService:
    return ProductService(session, default_registry(), settings, user_id)


def track(session: Session, user_id: int, index: int, settings: Settings) -> None:
    url = f"https://shop.example.com/p/quota-{index}"
    respx.get(url).mock(return_value=httpx.Response(200, html=load("jsonld_in_stock.html")))
    service(session, user_id, settings).add(url)


class TestListingCeiling:
    def test_the_last_allowed_listing_succeeds(self, db_session: Session, member: int) -> None:
        settings = capped(max_listings_per_user=3)
        for index in range(3):
            track(db_session, member, index, settings)

        assert service(db_session, member, settings).list().total == 3

    def test_one_beyond_the_ceiling_is_refused(
        self, db_session: Session, member: int
    ) -> None:
        settings = capped(max_listings_per_user=2)
        for index in range(2):
            track(db_session, member, index, settings)

        with pytest.raises(QuotaExceededError) as raised:
            track(db_session, member, 99, settings)

        # The message names the limit, so the person hitting it knows what to change.
        assert "2" in str(raised.value)
        assert "tracked listings" in str(raised.value)

    def test_joining_a_listing_someone_else_tracks_still_counts(
        self, db_session: Session, member: int
    ) -> None:
        """The ceiling is on what an account *watches*, not on what it created.

        Otherwise it is trivially avoided: let somebody else add the listing, then
        subscribe to it for free.
        """
        settings = capped(max_listings_per_user=1)
        other = int(user_service.create_user(db_session, email="other@example.com").user.id)
        url = "https://shop.example.com/p/shared-quota"
        respx.get(url).mock(return_value=httpx.Response(200, html=load("jsonld_in_stock.html")))
        service(db_session, other, settings).add(url)

        track(db_session, member, 0, settings)  # member reaches their limit

        with pytest.raises(QuotaExceededError):
            service(db_session, member, settings).add(url)

    def test_removing_one_frees_a_slot(self, db_session: Session, member: int) -> None:
        settings = capped(max_listings_per_user=1)
        track(db_session, member, 0, settings)
        listing = service(db_session, member, settings).list().items[0]

        service(db_session, member, settings).remove(listing.id)

        track(db_session, member, 1, settings)  # no longer refused


class TestGroupAndAlertCeilings:
    def test_groups_are_capped(self, db_session: Session, member: int) -> None:
        settings = capped(max_groups_per_user=2)
        for index in range(2):
            group_service.create_group(
                db_session, user_id=member, slug=f"g{index}", name=f"G{index}",
                settings=settings,
            )

        with pytest.raises(QuotaExceededError, match="product groups"):
            group_service.create_group(
                db_session, user_id=member, slug="g99", name="G99", settings=settings
            )

    def test_alerts_are_capped(self, db_session: Session, member: int) -> None:
        settings = capped(max_alerts_per_user=1)
        for index in range(2):
            track(db_session, member, index, settings)
        listings = service(db_session, member, settings).list().items

        alerts = AlertService(db_session, member, settings)
        alerts.add(listings[0].id, RuleType.PRICE_DROPPED)

        with pytest.raises(QuotaExceededError, match="alert rules"):
            alerts.add(listings[1].id, RuleType.PRICE_DROPPED)


class TestAdminsAreExempt:
    """The whole of what ``is_admin`` means.

    Before this it was recorded, displayed in ``users list``, and enforced nowhere -- a flag
    implying permissions that did not exist, which is worse than having no flag at all.
    """

    def test_an_admin_passes_the_listing_ceiling(
        self, db_session: Session, admin: int
    ) -> None:
        settings = capped(max_listings_per_user=1)
        for index in range(3):
            track(db_session, admin, index, settings)

        assert service(db_session, admin, settings).list().total == 3

    def test_an_admin_passes_the_group_ceiling(
        self, db_session: Session, admin: int
    ) -> None:
        settings = capped(max_groups_per_user=1)
        for index in range(3):
            group_service.create_group(
                db_session, user_id=admin, slug=f"a{index}", name=f"A{index}",
                settings=settings,
            )

    def test_an_admin_passes_the_alert_ceiling(
        self, db_session: Session, admin: int
    ) -> None:
        settings = capped(max_listings_per_user=99, max_alerts_per_user=1)
        for index in range(2):
            track(db_session, admin, index, settings)
        listings = service(db_session, admin, settings).list().items

        alerts = AlertService(db_session, admin, settings)
        alerts.add(listings[0].id, RuleType.PRICE_DROPPED)
        alerts.add(listings[1].id, RuleType.PRICE_DROPPED)

        assert alerts.list().total == 2

    def test_an_ordinary_account_is_not_exempt(
        self, db_session: Session, member: int
    ) -> None:
        assert not user_service.exempt_from_quota(db_session, member)

    def test_being_admin_does_not_exempt_anyone_from_pacing(self) -> None:
        """Deliberately: quotas are a courtesy ceiling, pacing protects the retailer.

        An admin who could also skip the throttle would be able to hammer a shop, which is
        the behaviour that got one to stop answering this machine in the first place.
        """
        from product_tracker.scheduler import throttle

        source = throttle.SharedStoreGuard.before.__doc__ or ""
        assert "admin" not in source.lower()
        # And the guard takes no user at all: it cannot know who is asking.
        assert "user" not in throttle.SharedStoreGuard.before.__code__.co_varnames
