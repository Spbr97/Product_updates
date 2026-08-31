"""Domain invariants.

The rule that matters most here: a failure to extract a price must never be reported as
"out of stock". That confusion is the classic price-tracker bug, so it is pinned down by
tests from the start.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from product_tracker.domain.enums import Availability, FetchMethod, FetchOutcome
from product_tracker.domain.models import FetchResult


class TestFetchOutcome:
    @pytest.mark.parametrize(
        "outcome",
        [FetchOutcome.OK, FetchOutcome.OUT_OF_STOCK, FetchOutcome.UNAVAILABLE],
    )
    def test_determined_states_are_successes(self, outcome: FetchOutcome) -> None:
        """We learned the truth, even when there is no price to record."""
        assert outcome.is_success

    @pytest.mark.parametrize(
        "outcome",
        [
            FetchOutcome.PRICE_NOT_FOUND,
            FetchOutcome.TIMEOUT,
            FetchOutcome.HTTP_ERROR,
            FetchOutcome.BLOCKED,
            FetchOutcome.PAGE_STRUCTURE,
            FetchOutcome.ERROR,
        ],
    )
    def test_undetermined_states_are_not_successes(self, outcome: FetchOutcome) -> None:
        assert not outcome.is_success

    @pytest.mark.parametrize(
        "outcome", [FetchOutcome.TIMEOUT, FetchOutcome.HTTP_ERROR, FetchOutcome.ERROR]
    )
    def test_transient_outcomes_are_worth_retrying(self, outcome: FetchOutcome) -> None:
        assert outcome.is_transient

    @pytest.mark.parametrize(
        "outcome",
        [
            FetchOutcome.BLOCKED,
            FetchOutcome.PAGE_STRUCTURE,
            FetchOutcome.PRICE_NOT_FOUND,
            FetchOutcome.OK,
            FetchOutcome.OUT_OF_STOCK,
        ],
    )
    def test_structural_outcomes_are_not_retried(self, outcome: FetchOutcome) -> None:
        """Retrying a blocked or malformed page immediately cannot help."""
        assert not outcome.is_transient


class TestFetchResultInvariants:
    def test_ok_requires_a_price(self) -> None:
        with pytest.raises(ValueError, match="requires a price"):
            FetchResult(outcome=FetchOutcome.OK, currency="INR")

    def test_price_requires_a_currency(self) -> None:
        with pytest.raises(ValueError, match="requires a currency"):
            FetchResult(outcome=FetchOutcome.OK, price=Decimal("100"))

    def test_valid_ok_result(self) -> None:
        result = FetchResult(
            outcome=FetchOutcome.OK,
            availability=Availability.IN_STOCK,
            price=Decimal("69999.00"),
            currency="INR",
            name="Apple iPhone 17",
            fetch_method=FetchMethod.HTTP,
            http_status=200,
        )
        assert result.succeeded
        assert result.price == Decimal("69999.00")


class TestAvailabilityIsIndependentOfExtraction:
    def test_default_availability_is_unknown(self) -> None:
        assert FetchResult(outcome=FetchOutcome.PRICE_NOT_FOUND).availability is (
            Availability.UNKNOWN
        )

    @pytest.mark.parametrize(
        "outcome",
        [
            FetchOutcome.PRICE_NOT_FOUND,
            FetchOutcome.TIMEOUT,
            FetchOutcome.HTTP_ERROR,
            FetchOutcome.BLOCKED,
            FetchOutcome.PAGE_STRUCTURE,
            FetchOutcome.ERROR,
        ],
    )
    def test_failures_never_claim_out_of_stock(self, outcome: FetchOutcome) -> None:
        """A failed read means we do not know the stock state -- not that stock is zero."""
        result = FetchResult.failure(outcome, "could not read the page")

        assert result.availability is Availability.UNKNOWN
        assert result.availability is not Availability.OUT_OF_STOCK
        assert not result.succeeded

    def test_out_of_stock_is_a_positive_finding(self) -> None:
        """Reporting OUT_OF_STOCK requires an explicit signal, carried as its own outcome."""
        result = FetchResult(
            outcome=FetchOutcome.OUT_OF_STOCK,
            availability=Availability.OUT_OF_STOCK,
            name="Apple iPhone 17",
            fetch_method=FetchMethod.HTTP,
        )
        assert result.succeeded
        assert result.price is None

    def test_failure_records_diagnostics(self) -> None:
        result = FetchResult.failure(
            FetchOutcome.HTTP_ERROR,
            "503 from origin",
            fetch_method=FetchMethod.HTTP,
            http_status=503,
        )
        assert result.http_status == 503
        assert result.message == "503 from origin"
        assert result.fetch_method is FetchMethod.HTTP
