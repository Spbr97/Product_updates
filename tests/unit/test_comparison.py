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
from product_tracker.services.comparison import cell_for_product

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
        cell = cell_for_product(make_product(), last_check=(CheckStatus.SUCCESS.value, None), now=NOW)
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


class TestStaleness:
    def test_a_recent_check_is_not_stale(self) -> None:
        cell = cell_for_product(
            make_product(checked=NOW - timedelta(hours=1)),
            stale_after=timedelta(hours=6),
            now=NOW,
        )
        assert not cell.is_stale

    def test_an_old_check_is_flagged(self) -> None:
        cell = cell_for_product(
            make_product(checked=NOW - timedelta(days=2)),
            stale_after=timedelta(hours=6),
            now=NOW,
        )
        assert cell.is_stale
        # Still a real price -- stale is a caveat, not a failure.
        assert cell.status is CellStatus.OK


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
