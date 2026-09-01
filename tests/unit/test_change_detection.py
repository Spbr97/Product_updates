"""Change detection: what a check is allowed to record.

The governing rule is that we only record what we learned. A failed check teaches us
nothing, so it must not write history -- otherwise a network hiccup would look like the
product going "unknown" and coming back.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from product_tracker.domain.enums import Availability, FetchMethod, FetchOutcome
from product_tracker.domain.models import FetchResult
from product_tracker.services.change_detection import (
    detect_availability_change,
    detect_price_change,
)


def ok(price: str | None = "100", currency: str = "INR", availability=Availability.IN_STOCK):  # type: ignore[no-untyped-def]
    if price is None:
        return FetchResult(
            outcome=FetchOutcome.OUT_OF_STOCK,
            availability=availability,
            fetch_method=FetchMethod.HTTP,
        )
    return FetchResult(
        outcome=FetchOutcome.OK,
        availability=availability,
        price=Decimal(price),
        currency=currency,
        fetch_method=FetchMethod.HTTP,
    )


class TestPriceRecording:
    def test_first_observation_is_recorded_but_is_not_a_change(self) -> None:
        outcome = detect_price_change(ok("100"), None, None)

        assert outcome.should_record
        assert not outcome.changed
        assert outcome.is_first_observation

    def test_a_different_price_is_recorded_and_is_a_change(self) -> None:
        outcome = detect_price_change(ok("90"), Decimal("100"), "INR")

        assert outcome.should_record
        assert outcome.changed
        assert outcome.previous == Decimal("100")
        assert outcome.current == Decimal("90")

    def test_an_unchanged_price_is_not_recorded(self) -> None:
        """Repeating the same number would grow the series without adding information."""
        outcome = detect_price_change(ok("100"), Decimal("100"), "INR")

        assert not outcome.should_record
        assert not outcome.changed

    def test_trailing_zeros_do_not_count_as_a_change(self) -> None:
        """Decimal('100.00') == Decimal('100'); the series must not record a fake move."""
        outcome = detect_price_change(ok("100.00"), Decimal("100"), "INR")
        assert not outcome.should_record

    def test_a_currency_switch_is_recorded_even_at_the_same_number(self) -> None:
        """100 USD is not 100 INR -- the value changed even though the digits did not."""
        outcome = detect_price_change(ok("100", "USD"), Decimal("100"), "INR")

        assert outcome.should_record
        assert outcome.changed

    @pytest.mark.parametrize(
        "outcome_kind",
        [
            FetchOutcome.BLOCKED,
            FetchOutcome.TIMEOUT,
            FetchOutcome.HTTP_ERROR,
            FetchOutcome.PAGE_STRUCTURE,
            FetchOutcome.ERROR,
        ],
    )
    def test_failed_checks_record_nothing(self, outcome_kind: FetchOutcome) -> None:
        result = FetchResult.failure(outcome_kind, "nope")

        outcome = detect_price_change(result, Decimal("100"), "INR")

        assert not outcome.should_record
        assert not outcome.changed

    def test_price_not_found_records_nothing(self) -> None:
        """Page read, price missing. We did not learn the price, so we do not write one."""
        result = FetchResult(outcome=FetchOutcome.PRICE_NOT_FOUND, name="Thing")

        outcome = detect_price_change(result, Decimal("100"), "INR")

        assert not outcome.should_record

    def test_out_of_stock_has_no_price_to_record(self) -> None:
        outcome = detect_price_change(ok(None, availability=Availability.OUT_OF_STOCK), None, None)
        assert not outcome.should_record


class TestAvailabilityRecording:
    def test_first_observation_is_recorded_but_is_not_a_change(self) -> None:
        outcome = detect_availability_change(ok(), None)

        assert outcome.should_record
        assert not outcome.changed
        assert outcome.is_first_observation

    def test_a_transition_is_recorded(self) -> None:
        outcome = detect_availability_change(
            ok(None, availability=Availability.OUT_OF_STOCK), Availability.IN_STOCK
        )

        assert outcome.should_record
        assert outcome.changed
        assert outcome.previous is Availability.IN_STOCK
        assert outcome.current is Availability.OUT_OF_STOCK

    def test_no_transition_records_nothing(self) -> None:
        """One row per transition, not one per check."""
        outcome = detect_availability_change(ok(), Availability.IN_STOCK)

        assert not outcome.should_record
        assert not outcome.changed

    def test_back_in_stock_is_a_change(self) -> None:
        outcome = detect_availability_change(ok(), Availability.OUT_OF_STOCK)

        assert outcome.changed
        assert outcome.current is Availability.IN_STOCK

    @pytest.mark.parametrize(
        "outcome_kind",
        [FetchOutcome.BLOCKED, FetchOutcome.TIMEOUT, FetchOutcome.HTTP_ERROR],
    )
    def test_a_failed_check_does_not_record_unknown(
        self, outcome_kind: FetchOutcome
    ) -> None:
        """Otherwise every network hiccup would log a transition into "unknown"."""
        result = FetchResult.failure(outcome_kind, "nope")

        outcome = detect_availability_change(result, Availability.IN_STOCK)

        assert not outcome.should_record
        assert not outcome.changed

    def test_price_not_found_does_not_record_availability(self) -> None:
        result = FetchResult(outcome=FetchOutcome.PRICE_NOT_FOUND, name="Thing")

        assert not detect_availability_change(result, Availability.IN_STOCK).should_record

    def test_unknown_from_a_successful_read_is_recorded(self) -> None:
        """A page with a price but no stock statement genuinely tells us "unknown"."""
        result = ok("100", availability=Availability.UNKNOWN)

        outcome = detect_availability_change(result, Availability.IN_STOCK)

        assert outcome.should_record
        assert outcome.current is Availability.UNKNOWN

    def test_listing_gone_is_recorded(self) -> None:
        result = FetchResult(
            outcome=FetchOutcome.UNAVAILABLE, availability=Availability.UNAVAILABLE
        )

        outcome = detect_availability_change(result, Availability.IN_STOCK)

        assert outcome.should_record
        assert outcome.current is Availability.UNAVAILABLE
