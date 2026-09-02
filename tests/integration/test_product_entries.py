"""Product Entries: one logical product, one listing per retailer.

The invariant every test here defends is a single sentence: *a price change must never look
like a new product.* The entry keeps its id through price moves, renames, and URL changes,
and every observation stays attached to the listing that actually saw it.

No network. Retailer pages are stubbed with respx, so nothing here depends on Amazon or
Flipkart being up.
"""

from __future__ import annotations

from collections.abc import Iterator
from decimal import Decimal

import httpx
import pytest
import respx
from sqlalchemy import select
from sqlalchemy.orm import Session
from tests.unit.test_adapters import load

from product_tracker.core.config import Settings, get_settings
from product_tracker.db.models import PriceHistory, RetailerListingUrlAudit
from product_tracker.domain.enums import ProductEntryStatus, TrackingStatus
from product_tracker.domain.errors import (
    DuplicateListingError,
    InvalidStoreURLError,
    NotFoundError,
    ValidationError,
)
from product_tracker.services import user_service
from product_tracker.services.product_entry_service import (
    ListingInput,
    ProductEntryService,
)
from product_tracker.stores.registry import default_registry

pytestmark = pytest.mark.db

AMAZON_URL = "https://www.amazon.in/dp/B0TESTAAAA"
AMAZON_URL_2 = "https://www.amazon.in/dp/B0TESTBBBB"
FLIPKART_URL = "https://www.flipkart.com/galaxy-s25/p/itmtest0001"
FLIPKART_URL_2 = "https://www.flipkart.com/galaxy-s25/p/itmtest0002"


@pytest.fixture(autouse=True)
def _respx_router() -> Iterator[None]:
    with respx.mock:
        yield


@pytest.fixture
def settings() -> Settings:
    return get_settings()


@pytest.fixture
def owner(db_session: Session) -> int:
    return int(user_service.create_user(db_session, email="owner@example.com").user.id)


@pytest.fixture
def service(db_session: Session, settings: Settings, owner: int) -> ProductEntryService:
    return ProductEntryService(db_session, default_registry(), settings, owner)


def stub(url: str, fixture: str = "jsonld_in_stock.html") -> None:
    respx.get(url).mock(return_value=httpx.Response(200, html=load(fixture)))


def stub_all() -> None:
    for url in (AMAZON_URL, AMAZON_URL_2, FLIPKART_URL, FLIPKART_URL_2):
        stub(url)


def make(
    service: ProductEntryService,
    *,
    name: str = "Samsung Galaxy S25 256GB",
    amazon_url: str = AMAZON_URL,
    flipkart_url: str = FLIPKART_URL,
):
    return service.create(
        name,
        amazon=ListingInput("Galaxy S25 on Amazon", amazon_url),
        flipkart=ListingInput("Galaxy S25 on Flipkart", flipkart_url),
    )


class TestCreation:
    def test_one_entry_with_one_listing_per_retailer(
        self, service: ProductEntryService
    ) -> None:
        stub_all()

        entry = make(service)

        assert entry.id is not None
        assert entry.status is ProductEntryStatus.ACTIVE
        stores = sorted(listing.store_slug for listing in entry.listings)
        assert stores == ["amazon-in", "flipkart"]

    def test_the_users_own_names_are_kept_not_the_scraped_ones(
        self, service: ProductEntryService
    ) -> None:
        """The fixture publishes its own title. Overwriting what the user typed with a
        scraped one would replace their wording with the shop's."""
        stub_all()

        entry = make(service)

        assert {listing.product_name for listing in entry.listings} == {
            "Galaxy S25 on Amazon",
            "Galaxy S25 on Flipkart",
        }

    def test_a_missing_name_is_refused(self, service: ProductEntryService) -> None:
        stub_all()
        with pytest.raises(ValidationError, match="product name is required"):
            make(service, name="   ")

    def test_a_missing_retailer_name_is_refused(
        self, service: ProductEntryService
    ) -> None:
        stub_all()
        with pytest.raises(ValidationError, match="Amazon product name"):
            service.create(
                "Galaxy S25",
                amazon=ListingInput("", AMAZON_URL),
                flipkart=ListingInput("ok", FLIPKART_URL),
            )


class TestRetailerFieldValidation:
    """The Amazon field takes Amazon links. Not a nicety: the two listings track
    independently, and filing one under the other would put a shop's prices in the wrong
    column for as long as the entry lived."""

    def test_a_flipkart_url_in_the_amazon_field_is_refused(
        self, service: ProductEntryService
    ) -> None:
        stub_all()
        with pytest.raises(InvalidStoreURLError, match="Amazon"):
            service.create(
                "Galaxy S25",
                amazon=ListingInput("wrong", FLIPKART_URL),
                flipkart=ListingInput("ok", FLIPKART_URL),
            )

    def test_an_amazon_url_in_the_flipkart_field_is_refused(
        self, service: ProductEntryService
    ) -> None:
        stub_all()
        with pytest.raises(InvalidStoreURLError, match="Flipkart"):
            service.create(
                "Galaxy S25",
                amazon=ListingInput("ok", AMAZON_URL),
                flipkart=ListingInput("wrong", AMAZON_URL),
            )

    def test_nothing_is_written_when_the_second_url_is_wrong(
        self, service: ProductEntryService, db_session: Session
    ) -> None:
        """Validation runs before the first insert. Otherwise a bad Flipkart URL would
        leave a tracked Amazon product and a half-built entry behind."""
        stub_all()
        with pytest.raises(InvalidStoreURLError):
            service.create(
                "Galaxy S25",
                amazon=ListingInput("ok", AMAZON_URL),
                flipkart=ListingInput("wrong", AMAZON_URL),
            )

        assert service.list().total == 0


class TestDuplicates:
    def test_a_url_already_live_in_an_entry_is_a_conflict(
        self, service: ProductEntryService
    ) -> None:
        stub_all()
        first = make(service)

        with pytest.raises(DuplicateListingError) as raised:
            make(service, name="Another try", flipkart_url=FLIPKART_URL_2)

        # The message names the entry, so the user is not left hunting their own list.
        assert str(first.id) in str(raised.value)

    def test_another_user_may_track_the_same_url(
        self, db_session: Session, settings: Settings, service: ProductEntryService
    ) -> None:
        """Listings are per account. The same public URL is not a shared resource."""
        stub_all()
        make(service)
        other = int(
            user_service.create_user(db_session, email="other@example.com").user.id
        )

        entry = make(
            ProductEntryService(db_session, default_registry(), settings, other),
            name="Their own",
        )

        assert entry.id is not None

    def test_a_deactivated_listing_does_not_block_re_adding(
        self, service: ProductEntryService
    ) -> None:
        """Which is exactly why the unique index is partial."""
        stub_all()
        entry = make(service)
        amazon = next(x for x in entry.listings if x.store_slug == "amazon-in")
        service.deactivate_listing(entry.id, amazon.id)

        again = make(service, name="Second entry", flipkart_url=FLIPKART_URL_2)

        assert again.id != entry.id


class TestUpdating:
    def test_renaming_preserves_the_id(self, service: ProductEntryService) -> None:
        stub_all()
        entry = make(service)

        renamed = service.update(entry.id, canonical_name="Galaxy S25 (256 GB)")

        assert renamed.id == entry.id
        assert renamed.canonical_name == "Galaxy S25 (256 GB)"

    def test_renaming_a_listing_touches_no_history(
        self, service: ProductEntryService, db_session: Session
    ) -> None:
        stub_all()
        entry = make(service)
        amazon = next(x for x in entry.listings if x.store_slug == "amazon-in")
        before = amazon.product_id

        service.update_listing(entry.id, amazon.id, product_name="New label")

        assert amazon.product_id == before
        assert amazon.product_name == "New label"


class TestUrlChange:
    """A URL change re-points; it never rewrites.

    The old prices were genuinely observed at the old URL. Rewriting them as though they
    came from the new one would be the tidiest possible lie.
    """

    def test_the_listing_and_entry_keep_their_ids(
        self, service: ProductEntryService
    ) -> None:
        stub_all()
        entry = make(service)
        amazon = next(x for x in entry.listings if x.store_slug == "amazon-in")

        updated = service.update_listing(entry.id, amazon.id, url=AMAZON_URL_2)

        assert updated.id == amazon.id
        assert service.get(entry.id).id == entry.id

    def test_it_points_at_a_new_tracking_target(
        self, service: ProductEntryService
    ) -> None:
        stub_all()
        entry = make(service)
        amazon = next(x for x in entry.listings if x.store_slug == "amazon-in")
        old_product = amazon.product_id

        service.update_listing(entry.id, amazon.id, url=AMAZON_URL_2)

        assert amazon.product_id != old_product

    def test_the_old_observations_are_left_exactly_as_they_were(
        self, service: ProductEntryService, db_session: Session
    ) -> None:
        stub_all()
        entry = make(service)
        amazon = next(x for x in entry.listings if x.store_slug == "amazon-in")
        old_product = amazon.product_id
        db_session.add(
            PriceHistory(
                product_id=old_product, price=Decimal("82900.00"), currency="INR"
            )
        )
        db_session.flush()

        service.update_listing(entry.id, amazon.id, url=AMAZON_URL_2)

        rows = (
            db_session.execute(
                select(PriceHistory).where(PriceHistory.product_id == old_product)
            )
            .scalars()
            .all()
        )
        assert [row.price for row in rows] == [Decimal("82900.00")]

    def test_the_move_is_recorded(
        self, service: ProductEntryService, db_session: Session
    ) -> None:
        stub_all()
        entry = make(service)
        amazon = next(x for x in entry.listings if x.store_slug == "amazon-in")

        service.update_listing(entry.id, amazon.id, url=AMAZON_URL_2)

        audit = (
            db_session.execute(
                select(RetailerListingUrlAudit).where(
                    RetailerListingUrlAudit.retailer_listing_id == amazon.id
                )
            )
            .scalars()
            .one()
        )
        assert audit.old_url == AMAZON_URL
        assert audit.new_url == AMAZON_URL_2

    def test_the_new_url_must_be_the_same_retailer(
        self, service: ProductEntryService
    ) -> None:
        """An entry's Amazon column must stay Amazon, or it quietly starts showing
        someone else's prices."""
        stub_all()
        entry = make(service)
        amazon = next(x for x in entry.listings if x.store_slug == "amazon-in")

        with pytest.raises(InvalidStoreURLError):
            service.update_listing(entry.id, amazon.id, url=FLIPKART_URL_2)

    def test_re_pointing_at_the_same_url_is_a_no_op(
        self, service: ProductEntryService, db_session: Session
    ) -> None:
        stub_all()
        entry = make(service)
        amazon = next(x for x in entry.listings if x.store_slug == "amazon-in")
        before = amazon.product_id

        service.update_listing(entry.id, amazon.id, url=AMAZON_URL)

        assert amazon.product_id == before
        assert db_session.execute(select(RetailerListingUrlAudit)).scalars().all() == []


class TestRemoval:
    def test_removing_one_retailer_leaves_the_other_and_the_entry(
        self, service: ProductEntryService
    ) -> None:
        stub_all()
        entry = make(service)
        amazon = next(x for x in entry.listings if x.store_slug == "amazon-in")

        service.deactivate_listing(entry.id, amazon.id)

        remaining = service.active_listings(entry.id)
        assert [x.store_slug for x in remaining] == ["flipkart"]
        assert service.get(entry.id).status is ProductEntryStatus.ACTIVE

    def test_a_removed_listing_stops_being_scheduled(
        self, service: ProductEntryService, db_session: Session
    ) -> None:
        """``tracking_status`` is what reconcile reads, so this is the switch it obeys."""
        from product_tracker.db.models import Product

        stub_all()
        entry = make(service)
        amazon = next(x for x in entry.listings if x.store_slug == "amazon-in")
        product_id = amazon.product_id

        service.deactivate_listing(entry.id, amazon.id)

        product = db_session.get(Product, product_id)
        assert product is not None
        assert product.tracking_status is TrackingStatus.PAUSED

    def test_removal_keeps_the_observations_readable(
        self, service: ProductEntryService, db_session: Session
    ) -> None:
        stub_all()
        entry = make(service)
        amazon = next(x for x in entry.listings if x.store_slug == "amazon-in")
        db_session.add(
            PriceHistory(
                product_id=amazon.product_id, price=Decimal("999.00"), currency="INR"
            )
        )
        db_session.flush()

        service.deactivate_listing(entry.id, amazon.id)

        rows = (
            db_session.execute(
                select(PriceHistory).where(PriceHistory.product_id == amazon.product_id)
            )
            .scalars()
            .all()
        )
        assert len(rows) == 1

    def test_archiving_retires_the_entry_and_every_listing(
        self, service: ProductEntryService
    ) -> None:
        stub_all()
        entry = make(service)

        service.archive(entry.id)

        archived = service.get(entry.id)
        assert archived.status is ProductEntryStatus.ARCHIVED
        assert archived.deleted_at is not None
        assert service.active_listings(entry.id) == []


class TestTrackingState:
    def test_pausing_stops_both_retailers(
        self, service: ProductEntryService, db_session: Session
    ) -> None:
        from product_tracker.db.models import Product

        stub_all()
        entry = make(service)

        service.set_tracking(entry.id, active=False)

        for listing in service.active_listings(entry.id):
            product = db_session.get(Product, listing.product_id)
            assert product is not None
            assert product.tracking_status is TrackingStatus.PAUSED

    def test_resuming_starts_them_again(
        self, service: ProductEntryService, db_session: Session
    ) -> None:
        from product_tracker.db.models import Product

        stub_all()
        entry = make(service)
        service.set_tracking(entry.id, active=False)

        service.set_tracking(entry.id, active=True)

        for listing in service.active_listings(entry.id):
            product = db_session.get(Product, listing.product_id)
            assert product is not None
            assert product.tracking_status is TrackingStatus.ACTIVE

    def test_product_ids_targets_one_retailer_when_asked(
        self, service: ProductEntryService
    ) -> None:
        stub_all()
        entry = make(service)
        amazon = next(x for x in entry.listings if x.store_slug == "amazon-in")

        assert service.product_ids(entry.id, listing_id=amazon.id) == [amazon.product_id]
        assert len(service.product_ids(entry.id)) == 2


class TestOwnership:
    def test_another_users_entry_is_not_found(
        self, db_session: Session, settings: Settings, service: ProductEntryService
    ) -> None:
        """Not found rather than forbidden: a 403 confirms the id exists, and ids are
        sequential."""
        stub_all()
        entry = make(service)
        other = int(
            user_service.create_user(db_session, email="nosy@example.com").user.id
        )
        theirs = ProductEntryService(db_session, default_registry(), settings, other)

        with pytest.raises(NotFoundError):
            theirs.get(entry.id)

    def test_another_users_entry_cannot_be_archived(
        self, db_session: Session, settings: Settings, service: ProductEntryService
    ) -> None:
        stub_all()
        entry = make(service)
        other = int(
            user_service.create_user(db_session, email="nosy2@example.com").user.id
        )
        theirs = ProductEntryService(db_session, default_registry(), settings, other)

        with pytest.raises(NotFoundError):
            theirs.archive(entry.id)

    def test_a_listing_from_another_entry_is_not_reachable(
        self, service: ProductEntryService
    ) -> None:
        """Ownership is established on the entry; the listing must stay inside it."""
        stub_all()
        first = make(service)
        second = make(
            service,
            name="Second",
            amazon_url=AMAZON_URL_2,
            flipkart_url=FLIPKART_URL_2,
        )
        foreign = second.listings[0]

        with pytest.raises(NotFoundError):
            service.update_listing(first.id, foreign.id, product_name="hijack")


class TestListing:
    def test_entries_are_listed_newest_first(self, service: ProductEntryService) -> None:
        stub_all()
        make(service, name="First")
        make(service, name="Second", amazon_url=AMAZON_URL_2, flipkart_url=FLIPKART_URL_2)

        names = [item.canonical_name for item in service.list().items]

        assert names == ["Second", "First"]

    def test_archived_entries_can_be_filtered_out(
        self, service: ProductEntryService
    ) -> None:
        stub_all()
        kept = make(service, name="Kept")
        gone = make(
            service, name="Gone", amazon_url=AMAZON_URL_2, flipkart_url=FLIPKART_URL_2
        )
        service.archive(gone.id)

        active = service.list(status=ProductEntryStatus.ACTIVE)

        assert [item.id for item in active.items] == [kept.id]
        assert active.total == 1
