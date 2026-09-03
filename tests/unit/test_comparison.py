"""How a listing becomes one square of the comparison grid.

This is where the project's central invariant has to survive contact with a UI. "No price"
has several causes, and the grid must keep them apart: a shop that blocked us has said
nothing about stock, while a shop reporting sold out has said a great deal. Collapsing them
into one empty cell would reintroduce, at the last possible moment, exactly the lie the
data model spends so much effort refusing to tell.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from product_tracker.db.models import Product
from product_tracker.domain.enums import Availability, CellStatus, CheckStatus, FetchOutcome
from product_tracker.domain.models import ComparisonCell, ComparisonRow
from product_tracker.services.comparison import _one_per_store, cell_for_product

NOW = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)


def make_product(
    *,
    price: Decimal | None = Decimal("82900.00"),
    availability: Availability = Availability.IN_STOCK,
    checked: datetime | None = NOW,
) -> Product:
    """An unsaved listing. ``cell_for_product`` reads scalars only, so no database."""
    return Product(
        id=1,
        url="https://shop.example/item",
        url_canonical="https://shop.example/item",
        store_id=1,
        current_price=price,
        currency="INR" if price is not None else None,
        availability=availability,
        last_checked_at=checked,
    )


class TestBlockedIsNotOutOfStock:
    """The distinction the whole enum exists for."""

    def test_a_block_reports_blocked(self) -> None:
        cell = cell_for_product(
            make_product(price=None, availability=Availability.UNKNOWN),
            last_check=(CheckStatus.FAILED.value, FetchOutcome.BLOCKED.value),
            now=NOW,
        )
        assert cell.status is CellStatus.BLOCKED
        assert cell.availability is Availability.UNKNOWN

    def test_a_block_never_reports_out_of_stock(self) -> None:
        cell = cell_for_product(
            make_product(price=None, availability=Availability.UNKNOWN),
            last_check=(CheckStatus.FAILED.value, FetchOutcome.BLOCKED.value),
            now=NOW,
        )
        assert cell.status is not CellStatus.OUT_OF_STOCK

    def test_block_wins_over_a_stale_out_of_stock_reading(self) -> None:
        """A listing last seen sold out, then blocked, must report the block.

        Otherwise the grid keeps asserting "sold out" on the strength of a reading the
        shop has since stopped letting us take.
        """
        cell = cell_for_product(
            make_product(price=None, availability=Availability.OUT_OF_STOCK),
            last_check=(CheckStatus.FAILED.value, FetchOutcome.BLOCKED.value),
            now=NOW,
        )
        assert cell.status is CellStatus.BLOCKED


class TestPriceFailureIsNotOutOfStock:
    def test_unreadable_price_reports_no_price(self) -> None:
        cell = cell_for_product(
            make_product(price=None, availability=Availability.UNKNOWN),
            last_check=(CheckStatus.PARTIAL.value, FetchOutcome.PRICE_NOT_FOUND.value),
            now=NOW,
        )
        assert cell.status is CellStatus.NO_PRICE

    def test_needs_location_reports_no_price(self) -> None:
        """A shop that will not quote a price for our delivery area is a "no price"
        cell, not a failure and emphatically not a sold-out one. The check succeeded at
        everything except the one thing we wanted, which is exactly what NO_PRICE means.

        This is a regression guard rather than new behaviour: ``needs_location`` is
        deliberately absent from ``_FAILURE_OUTCOMES``, and adding it there would turn a
        readable page into a reported failure.
        """
        cell = cell_for_product(
            make_product(price=None, availability=Availability.UNKNOWN),
            last_check=(CheckStatus.PARTIAL.value, FetchOutcome.NEEDS_LOCATION.value),
            now=NOW,
        )
        assert cell.status is CellStatus.NO_PRICE
        assert cell.status is not CellStatus.OUT_OF_STOCK

    @pytest.mark.parametrize(
        "outcome",
        [
            FetchOutcome.TIMEOUT.value,
            FetchOutcome.HTTP_ERROR.value,
            FetchOutcome.ERROR.value,
            FetchOutcome.PAGE_STRUCTURE.value,
        ],
    )
    def test_fetch_failures_report_failed(self, outcome: str) -> None:
        cell = cell_for_product(
            make_product(price=None, availability=Availability.UNKNOWN),
            last_check=(CheckStatus.FAILED.value, outcome),
            now=NOW,
        )
        assert cell.status is CellStatus.FAILED


class TestOrdinaryReadings:
    def test_a_price_reports_ok(self) -> None:
        cell = cell_for_product(
            make_product(), last_check=(CheckStatus.SUCCESS.value, None), now=NOW
        )
        assert cell.status is CellStatus.OK
        assert cell.has_price
        assert cell.price == Decimal("82900.00")

    def test_sold_out_reports_out_of_stock(self) -> None:
        cell = cell_for_product(
            make_product(availability=Availability.OUT_OF_STOCK),
            last_check=(CheckStatus.SUCCESS.value, FetchOutcome.OUT_OF_STOCK.value),
            now=NOW,
        )
        assert cell.status is CellStatus.OUT_OF_STOCK
        # A sold-out cell is never counted as a price you could pay.
        assert not cell.has_price

    def test_never_checked_is_distinct_from_failed(self) -> None:
        cell = cell_for_product(make_product(price=None, checked=None), now=NOW)
        assert cell.status is CellStatus.NEVER_CHECKED


SUCCEEDED = (CheckStatus.SUCCESS.value, None)


class TestStaleness:
    def test_a_recent_confirmed_check_is_not_stale(self) -> None:
        cell = cell_for_product(
            make_product(checked=NOW - timedelta(hours=1)),
            last_check=SUCCEEDED,
            stale_after=timedelta(hours=6),
            now=NOW,
        )
        assert not cell.is_stale

    def test_an_old_check_is_flagged(self) -> None:
        cell = cell_for_product(
            make_product(checked=NOW - timedelta(days=2)),
            last_check=SUCCEEDED,
            stale_after=timedelta(hours=6),
            now=NOW,
        )
        assert cell.is_stale
        # Still a real price -- stale is a caveat, not a failure.
        assert cell.status is CellStatus.OK

    def test_a_price_the_latest_check_could_not_confirm_is_flagged(self) -> None:
        """A price read last week, whose check a minute ago came back without one.

        By age it is fresh, and showing it as a confident current figure is the same class
        of overclaim as calling a failed extraction "out of stock". This is the real case
        that produced it: an Amazon listing kept displaying a price from a mis-extraction
        after the checker had stopped being able to read one.
        """
        cell = cell_for_product(
            make_product(checked=NOW - timedelta(minutes=1)),
            last_check=(CheckStatus.PARTIAL.value, FetchOutcome.PRICE_NOT_FOUND.value),
            stale_after=timedelta(hours=6),
            now=NOW,
        )
        assert cell.status is CellStatus.OK
        assert cell.is_stale

    def test_an_unchecked_price_is_not_presented_as_confirmed(self) -> None:
        cell = cell_for_product(make_product(), stale_after=timedelta(hours=6), now=NOW)
        assert cell.is_stale


class TestPriceMovement:
    def test_a_drop_is_negative(self) -> None:
        cell = cell_for_product(
            make_product(price=Decimal("79900")), previous_price=Decimal("82900"), now=NOW
        )
        assert cell.price_delta == Decimal("-3000")

    def test_no_previous_price_means_no_movement(self) -> None:
        assert cell_for_product(make_product(), now=NOW).price_delta is None


class TestRowArithmetic:
    """The summary line a shopper actually reads."""

    @staticmethod
    def row(**prices: str | None) -> ComparisonRow:
        cells = {}
        for slug, value in prices.items():
            if value is None:
                cells[slug] = ComparisonCell(status=CellStatus.NOT_TRACKED)
            else:
                cells[slug] = ComparisonCell(
                    status=CellStatus.OK, price=Decimal(value), currency="INR"
                )
        return ComparisonRow(variant_id=1, label="256GB / Black", cells=cells)

    def test_best_price_is_the_cheapest(self) -> None:
        row = self.row(flipkart="82900", croma="85900", reliance="83500")
        assert row.best_price == Decimal("82900")
        assert row.best_store_slugs == ("flipkart",)

    def test_ties_report_every_store_at_that_price(self) -> None:
        """Hiding a tie would send someone to one shop when two are equal."""
        row = self.row(flipkart="82900", reliance="82900", croma="85900")
        assert set(row.best_store_slugs) == {"flipkart", "reliance"}

    def test_spread_is_what_shopping_around_is_worth(self) -> None:
        row = self.row(flipkart="82900", croma="85900")
        assert row.spread == Decimal("3000")

    def test_a_single_price_has_no_spread(self) -> None:
        assert self.row(flipkart="82900", croma=None).spread is None

    def test_untracked_and_blocked_cells_never_count_as_prices(self) -> None:
        row = ComparisonRow(
            variant_id=1,
            label="256GB / Black",
            cells={
                "croma": ComparisonCell(status=CellStatus.BLOCKED, price=Decimal("1")),
                "flipkart": ComparisonCell(
                    status=CellStatus.OUT_OF_STOCK, price=Decimal("2"), currency="INR"
                ),
                "reliance": ComparisonCell(
                    status=CellStatus.OK, price=Decimal("82900"), currency="INR"
                ),
            },
        )
        # Only the genuinely buyable listing is a candidate.
        assert row.best_price == Decimal("82900")
        assert row.best_store_slugs == ("reliance",)


class TestOneListingPerShop:
    """A shop can carry the same model twice; the grid has one square for it."""

    @staticmethod
    def listing(product_id: int, store_slug: str, price: str | None) -> Product:
        from product_tracker.db.models import Store

        product = Product(
            id=product_id,
            url=f"https://{store_slug}.example/p/{product_id}",
            url_canonical=f"https://{store_slug}.example/p/{product_id}",
            store_id=1,
            current_price=Decimal(price) if price else None,
            currency="INR" if price else None,
            availability=Availability.IN_STOCK,
            last_checked_at=NOW,
        )
        product.store = Store(
            id=1, slug=store_slug, name=store_slug.title(), domains=[], adapter_key="generic"
        )
        return product

    def test_the_cheapest_listing_wins_the_square(self) -> None:
        """That is the number a shopper acts on."""
        chosen = _one_per_store(
            [
                self.listing(1, "flipkart", "84999"),
                self.listing(2, "flipkart", "79999"),
            ]
        )
        assert chosen["flipkart"].id == 2

    def test_the_result_does_not_depend_on_row_order(self) -> None:
        """Built by comprehension this kept whichever came last, so a listing vanished
        from the comparison depending on query order -- silently, and only sometimes."""
        pair = [self.listing(1, "flipkart", "84999"), self.listing(2, "flipkart", "79999")]
        assert _one_per_store(pair)["flipkart"].id == _one_per_store(pair[::-1])["flipkart"].id

    def test_a_priced_listing_beats_an_unpriced_one(self) -> None:
        chosen = _one_per_store(
            [self.listing(1, "flipkart", None), self.listing(2, "flipkart", "79999")]
        )
        assert chosen["flipkart"].id == 2

    def test_an_unpriced_listing_never_displaces_a_priced_one(self) -> None:
        chosen = _one_per_store(
            [self.listing(1, "flipkart", "79999"), self.listing(2, "flipkart", None)]
        )
        assert chosen["flipkart"].id == 1

    def test_equal_prices_settle_deterministically(self) -> None:
        pair = [self.listing(7, "flipkart", "79999"), self.listing(3, "flipkart", "79999")]
        assert _one_per_store(pair)["flipkart"].id == 3
        assert _one_per_store(pair[::-1])["flipkart"].id == 3

    def test_different_shops_keep_their_own_squares(self) -> None:
        chosen = _one_per_store(
            [self.listing(1, "flipkart", "79999"), self.listing(2, "samsung", "84999")]
        )
        assert set(chosen) == {"flipkart", "samsung"}
