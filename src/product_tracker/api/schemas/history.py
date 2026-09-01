"""History and statistics response models."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field

from ...domain.enums import Availability


class PriceHistoryEntry(BaseModel):
    """One recorded price observation."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    price: Decimal
    currency: str
    observed_at: datetime
    check_execution_id: int | None = Field(
        default=None, description="The check that produced this observation."
    )


class AvailabilityHistoryEntry(BaseModel):
    """One recorded availability transition."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    availability: Availability
    observed_at: datetime
    check_execution_id: int | None = None


class PriceStatsResponse(BaseModel):
    """Aggregates over a product's recorded prices.

    Computed for a single currency -- the one of the most recent observation. If the
    listing has been priced in more than one, ``mixed_currency`` is true and older rows in
    other currencies are excluded rather than averaged into a meaningless number.
    """

    model_config = ConfigDict(from_attributes=True)

    currency: str
    observations: int
    current: Decimal | None
    lowest: Decimal | None
    highest: Decimal | None
    average: Decimal | None
    lowest_at: datetime | None = Field(
        default=None, description="When the lowest price was first seen."
    )
    highest_at: datetime | None = None
    first_observed_at: datetime | None = None
    changed_by: Decimal | None = Field(
        default=None, description="Current price minus the first recorded price."
    )
    changed_pct: Decimal | None = Field(
        default=None, description="That change as a percentage of the first recorded price."
    )
    mixed_currency: bool = False
