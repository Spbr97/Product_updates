"""Request and response models for Product Entries.

The response shape is deliberately per-retailer all the way down. A merged "current price"
across Amazon and Flipkart would be the one number a comparison tool must never invent, and
a merged history would splice two shops' observations into a series that never happened.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field

from ...domain.enums import (
    Availability,
    CheckStatus,
    ProductEntryStatus,
    TrackingStatus,
)


class ListingInputSchema(BaseModel):
    """One retailer's half of the Add Product form."""

    product_name: str = Field(
        min_length=1,
        max_length=200,
        description="What to call this product at this shop. Yours, not the shop's.",
    )
    url: str = Field(
        min_length=1,
        max_length=8192,
        description="The product page at this retailer. Must belong to that retailer.",
    )


class ProductEntryCreate(BaseModel):
    """Create one entry with one Amazon and one Flipkart listing.

    Both retailers are required in v1. An entry with one shop cannot compare anything,
    which is the whole point of the thing.
    """

    product_name: str = Field(
        min_length=1,
        max_length=200,
        description="The canonical name. Not unique -- two entries may share one.",
        examples=["Samsung Galaxy S25 256GB"],
    )
    amazon: ListingInputSchema
    flipkart: ListingInputSchema


class ProductEntryUpdate(BaseModel):
    """Rename an entry. Its id, listings and history are untouched."""

    canonical_name: str = Field(min_length=1, max_length=200)


class ListingUpdate(BaseModel):
    """Change a listing's name, its URL, or both.

    A URL change re-points the listing at a new tracking target and keeps the listing's id;
    observations already recorded stay attached to the URL that produced them.
    """

    product_name: str | None = Field(default=None, min_length=1, max_length=200)
    url: str | None = Field(default=None, min_length=1, max_length=8192)


class ListingResponse(BaseModel):
    """One retailer's listing, with whatever is currently known about it.

    ``price`` and ``availability`` are independent. A null price with
    ``availability="unknown"`` means we could not read it -- never that the product is out
    of stock. ``last_check_status`` says which of those happened.
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    store: str = Field(description="Store slug, e.g. 'amazon-in'.")
    store_name: str = Field(description="Display name, e.g. 'Amazon India'.")
    product_name: str = Field(description="The user's name for it at this shop.")
    url: str
    product_id: int = Field(description="The underlying tracking target.")
    price: Decimal | None = None
    currency: str | None = None
    availability: Availability
    tracking_status: TrackingStatus
    last_checked_at: datetime | None = None
    last_check_status: CheckStatus | None = Field(
        default=None, description="Outcome of the most recent attempt, if any."
    )
    last_check_error: str | None = Field(
        default=None, description="Why the last attempt did not produce a price."
    )
    is_active: bool = Field(description="False once this retailer has been removed.")
    deactivated_at: datetime | None = None


class ProductEntryResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    product_name: str = Field(description="The canonical name.")
    status: ProductEntryStatus
    listings: list[ListingResponse]
    created_at: datetime
    updated_at: datetime
    deleted_at: datetime | None = None


class ListingCheckResult(BaseModel):
    """What one retailer's check did.

    Reported per retailer because they are independent: Amazon failing says nothing about
    Flipkart, and collapsing them into one status would hide the half that worked.
    """

    listing_id: int
    store: str
    status: CheckStatus
    price: Decimal | None = None
    currency: str | None = None
    availability: Availability | None = None
    error_type: str | None = None
    error_detail: str | None = None


class EntryCheckResponse(BaseModel):
    """The outcome of checking an entry.

    Always 200, even when every retailer failed. A recorded failure is a successful
    observation of a failure, and a 500 here would say the API broke when it did not.
    """

    product_entry_id: int
    results: list[ListingCheckResult]


class PricePoint(BaseModel):
    price: Decimal
    currency: str
    observed_at: datetime


class AvailabilityPoint(BaseModel):
    availability: Availability
    observed_at: datetime


class ListingHistory(BaseModel):
    """One retailer's observations. Never merged with another's."""

    listing_id: int
    store: str
    store_name: str
    prices: list[PricePoint]
    availability: list[AvailabilityPoint]


class EntryHistoryResponse(BaseModel):
    product_entry_id: int
    listings: list[ListingHistory]


class ListingStats(BaseModel):
    """Statistics for one retailer.

    ``mixed_currency`` rather than a converted average: prices in different currencies are
    not the same unit, and averaging them would produce a number that means nothing.
    """

    listing_id: int
    store: str
    store_name: str
    currency: str | None = None
    observations: int = 0
    current: Decimal | None = None
    lowest: Decimal | None = None
    highest: Decimal | None = None
    average: Decimal | None = None
    lowest_at: datetime | None = None
    first_observed_at: datetime | None = None
    changed_by: Decimal | None = None
    mixed_currency: bool = False


class EntryStatsResponse(BaseModel):
    product_entry_id: int
    listings: list[ListingStats]
