"""The Amazon adapter.

The fixtures mirror the structure of a real amazon.in listing, including the parts that
made a naive adapter wrong: cross-sell tiles carrying their own prices *before* the product
in document order, and a buy box whose first ``.a-offscreen`` node is empty.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from tests.unit.test_adapters import load

from product_tracker.domain.enums import Availability, FetchMethod, FetchOutcome
from product_tracker.stores.amazon import AmazonAdapter, read_availability
from product_tracker.stores.http import FetchSuccess

URL = "https://www.amazon.in/Samsung-Snapdragon/dp/B0H3FN92VB?th=1"


def interpret(fixture: str, url: str = URL):  # type: ignore[no-untyped-def]
    return AmazonAdapter()._interpret(
        FetchSuccess(html=load(fixture), url=url, http_status=200), url, FetchMethod.HTTP
    )


class TestPriceComesFromTheBuyBox:
    def test_reads_the_product_price(self) -> None:
        result = interpret("amazon_product.html")

        assert result.outcome is FetchOutcome.OK
        assert result.price == Decimal("84999")
        assert result.currency == "INR"

    def test_a_cross_sell_tile_is_not_the_product_price(self) -> None:
        """The bug this adapter exists to prevent.

        A page-wide ``.a-price`` matches about fifteen "customers also bought" tiles, and
        the first of them priced an Rs 84,999 phone at Rs 61,480 -- reported as a success.
        """
        result = interpret("amazon_product.html")

        assert result.price != Decimal("61480.00")
        assert result.price == Decimal("84999")

    def test_an_empty_leading_price_node_does_not_hide_the_real_one(self) -> None:
        """Amazon's buy box leads with an empty ``.a-offscreen``.

        Taking only the first match per selector found nothing here and fell through to a
        selector that matched a recommendation tile.
        """
        assert interpret("amazon_product.html").price == Decimal("84999")

    def test_reads_the_title_and_asin(self) -> None:
        result = interpret("amazon_product.html")

        assert result.name is not None
        assert "Galaxy S25" in result.name
        # Not "Add to your order" -- the cross-sell heading that appears first.
        assert "Add to your order" not in result.name
        assert result.product_identifier == "B0H3FN92VB"


class TestAvailabilityIsReadFromWords:
    def test_in_stock(self) -> None:
        assert interpret("amazon_product.html").availability is Availability.IN_STOCK

    def test_currently_unavailable_is_out_of_stock(self) -> None:
        result = interpret("amazon_out_of_stock.html")

        assert result.outcome is FetchOutcome.OUT_OF_STOCK
        assert result.availability is Availability.OUT_OF_STOCK

    def test_an_unbuyable_listing_reports_no_price(self) -> None:
        """A price still on the page is not one that can be paid.

        Recording it would put an unbuyable number into price history, where every later
        statistic would treat it as a real offer.
        """
        assert interpret("amazon_out_of_stock.html").price is None

    def test_only_n_left_is_in_stock(self) -> None:
        """"Only 2 left in stock" contains "in stock", and also contains "stock".

        The negative phrases are tested first, so the ordering has to be right or a
        low-stock warning reads as unavailable.
        """
        result = interpret("amazon_low_stock.html")

        assert result.availability is Availability.IN_STOCK
        assert result.price == Decimal("79999")

    def test_unrecognised_wording_is_unknown_not_assumed(self) -> None:
        """Wording we have not seen tells us nothing, so it must not become a verdict."""
        result = interpret("amazon_unknown_wording.html")

        assert result.availability is Availability.UNKNOWN
        # The price is still real and still reported.
        assert result.price == Decimal("84999")
        assert result.outcome is FetchOutcome.OK


class TestReadAvailability:
    @pytest.mark.parametrize(
        ("wording", "expected"),
        [
            ("In stock", Availability.IN_STOCK),
            ("In stock soon.", Availability.IN_STOCK),
            ("Only 2 left in stock.", Availability.IN_STOCK),
            ("Usually dispatched in 3 days", Availability.IN_STOCK),
            ("Currently unavailable.", Availability.OUT_OF_STOCK),
            ("Temporarily out of stock.", Availability.OUT_OF_STOCK),
            ("Out of stock", Availability.OUT_OF_STOCK),
            ("Dispatches in 3 to 5 weeks", Availability.UNKNOWN),
            ("", Availability.UNKNOWN),
            (None, Availability.UNKNOWN),
        ],
    )
    def test_wording(self, wording: str | None, expected: Availability) -> None:
        assert read_availability(wording) is expected

    def test_out_of_stock_wins_over_the_substring_in_stock(self) -> None:
        """"out of stock" contains "in stock"; a naive check reports the opposite."""
        assert read_availability("Out of stock") is Availability.OUT_OF_STOCK


class TestMissingData:
    def test_a_listing_with_no_buy_box_reports_price_not_found(self) -> None:
        result = interpret("amazon_no_price.html")

        assert result.outcome is FetchOutcome.PRICE_NOT_FOUND
        assert result.price is None
        # Never out-of-stock merely because no price could be read.
        assert result.availability is Availability.UNKNOWN
        assert result.name is not None

    def test_an_unrecognisable_page_reports_page_structure(self) -> None:
        result = AmazonAdapter()._interpret(
            FetchSuccess(html="<html><body>nothing</body></html>", url=URL, http_status=200),
            URL,
            FetchMethod.HTTP,
        )
        assert result.outcome is FetchOutcome.PAGE_STRUCTURE


class TestAsin:
    @pytest.mark.parametrize(
        ("url", "expected"),
        [
            ("https://www.amazon.in/Some-Name/dp/B0H3FN92VB?th=1", "B0H3FN92VB"),
            ("https://www.amazon.in/dp/B0H3FN92VB", "B0H3FN92VB"),
            ("https://www.amazon.in/gp/product/B0H3FN92VB", "B0H3FN92VB"),
            ("https://www.amazon.in/s?k=galaxy+s25", None),
        ],
    )
    def test_extracts_the_asin_from_the_path(self, url: str, expected: str | None) -> None:
        assert AmazonAdapter()._asin(url) == expected


class TestUrlMatching:
    def test_handles_amazon_in(self) -> None:
        adapter = AmazonAdapter()
        assert adapter.can_handle_url("https://www.amazon.in/dp/B0H3FN92VB")
        assert adapter.can_handle_url("https://amazon.in/dp/B0H3FN92VB")

    def test_does_not_claim_other_stores(self) -> None:
        adapter = AmazonAdapter()
        assert not adapter.can_handle_url("https://www.flipkart.com/x/p/y")
        # A lookalike host must not match.
        assert not adapter.can_handle_url("https://notamazon.in/dp/B0H3FN92VB")
