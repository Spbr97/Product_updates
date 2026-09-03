"""Request and response models for products, stores, and checks."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from ...domain.enums import Availability, CheckStatus, FetchMethod, TrackingStatus


class ProductCreate(BaseModel):
    """Request body for tracking a new product.

    The URL is validated in the service layer, not here: the SSRF guard needs DNS
    resolution and the configured policy, neither of which belongs in a schema.
    """

    url: str = Field(
        min_length=1,
        max_length=8192,
        description="Product page URL (https:// or http://).",
        examples=["https://www.flipkart.com/apple-iphone-17-black-256-gb/p/itm6eb39da622cdd"],
    )
    check_interval_seconds: int | None = Field(
        default=None,
        ge=60,
        description="Override the global check interval for this product.",
    )


class ProductUpdate(BaseModel):
    """Fields on a tracked product that can be changed in place.

    Only the check interval for now. It is a required field, not optional: ``PATCH`` with
    an empty body would otherwise silently reset the interval. Pass ``null`` deliberately
    to return the product to the global default; the sub-minute floor is enforced by the
    service, not here, so its message stays in one place.
    """

    check_interval_seconds: int | None = Field(
        description="Seconds between scheduled checks; null restores the global default.",
    )


class StoreSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    slug: str
    name: str


class ProductResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    url: str
    store: StoreSummary
    name: str | None
    product_identifier: str | None
    image_url: str | None
    current_price: Decimal | None
    currency: str | None
    availability: Availability
    tracking_status: TrackingStatus
    check_interval_seconds: int | None
    last_checked_at: datetime | None
    last_success_at: datetime | None
    consecutive_failures: int
    created_at: datetime
    updated_at: datetime
    extra_metadata: dict[str, Any]


class CheckResponse(BaseModel):
    """The outcome of one check.

    ``availability`` and ``status`` are independent: a ``partial`` check (page readable,
    price missing) reports ``unknown`` availability rather than claiming out of stock.
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    product_id: int
    status: CheckStatus
    fetch_method: FetchMethod
    http_status: int | None
    extracted_price: Decimal | None
    extracted_currency: str | None
    availability_result: Availability | None
    duration_ms: int | None
    started_at: datetime
    finished_at: datetime | None
    error_type: str | None
    error_detail: str | None


class StoreResponse(BaseModel):
    """A supported retailer."""

    slug: str
    name: str
    domains: list[str]
    adapter: str = Field(description="Which adapter reads this store's pages.")
    is_fallback: bool = Field(
        description="True for the catch-all used when no named store matches the domain."
    )
