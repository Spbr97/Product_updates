"""Deciding what a check actually learned.

Two questions, kept separate because the answers differ:

* **Is this worth recording?** A first observation is worth recording but is not a change.
* **Is this a change?** Only when there was a previous value and the new one differs.

The rule that governs everything here: *we only record what we learned*. A check that
failed, or that read the page but found no price, teaches us nothing about that value, so
it writes no history. The attempt is still recorded in ``check_executions`` -- the
diagnostic trail and the observation trail are different things.

A currency switch counts as a price change worth recording: the number means something
different than it did before.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from ..domain.enums import Availability
from ..domain.models import RETRACTS_AVAILABILITY, FetchResult


@dataclass(frozen=True, slots=True)
class PriceOutcome:
    """What to do about the price this check produced."""

    should_record: bool
    changed: bool
    previous: Decimal | None
    current: Decimal | None
    previous_currency: str | None = None

    @property
    def is_first_observation(self) -> bool:
        return self.should_record and self.previous is None


@dataclass(frozen=True, slots=True)
class AvailabilityOutcome:
    """What to do about the availability this check produced."""

    should_record: bool
    changed: bool
    previous: Availability | None
    current: Availability

    @property
    def is_first_observation(self) -> bool:
        return self.should_record and self.previous is None


def detect_price_change(
    result: FetchResult,
    previous_price: Decimal | None,
    previous_currency: str | None,
) -> PriceOutcome:
    """Decide whether this check's price should be appended to history.

    Records on the first observation, on a different price, or on a currency switch.
    An unchanged price is deliberately not appended -- the check is already recorded in
    ``check_executions``, and repeating the same number would bloat the series without
    adding information.
    """
    current = result.price

    if not result.succeeded or current is None:
        # Nothing was learned about the price.
        return PriceOutcome(
            should_record=False,
            changed=False,
            previous=previous_price,
            current=None,
            previous_currency=previous_currency,
        )

    if previous_price is None:
        return PriceOutcome(
            should_record=True, changed=False, previous=None, current=current
        )

    currency_switched = (
        previous_currency is not None
        and result.currency is not None
        and previous_currency != result.currency
    )
    differs = current != previous_price or currency_switched

    return PriceOutcome(
        should_record=differs,
        changed=differs,
        previous=previous_price,
        current=current,
        previous_currency=previous_currency,
    )


def detect_availability_change(
    result: FetchResult, previous: Availability | None
) -> AvailabilityOutcome:
    """Decide whether this check's availability should be appended to history.

    Only successful checks contribute. A failed fetch reports ``UNKNOWN`` because it
    learned nothing, and writing that would falsely record a transition into "unknown"
    every time the network hiccupped -- and back out again on the next success.

    The single exception is a *retraction*: a result that does not merely fail to read an
    availability but positively establishes that the one on record was never about the
    product (see :mod:`product_tracker.stores.pincode`). That is something learned, and
    the transition out of a false reading is exactly what history should show -- otherwise
    the shop's mistaken "out of stock" stands as the last word for ever.
    """
    if result.raw_metadata.get(RETRACTS_AVAILABILITY):
        differs = previous is not None and previous is not Availability.UNKNOWN
        return AvailabilityOutcome(
            should_record=differs,
            changed=differs,
            previous=previous,
            current=Availability.UNKNOWN,
        )

    if not result.succeeded:
        return AvailabilityOutcome(
            should_record=False, changed=False, previous=previous, current=result.availability
        )

    current = result.availability

    if previous is None:
        return AvailabilityOutcome(
            should_record=True, changed=False, previous=None, current=current
        )

    differs = current != previous
    return AvailabilityOutcome(
        should_record=differs, changed=differs, previous=previous, current=current
    )
