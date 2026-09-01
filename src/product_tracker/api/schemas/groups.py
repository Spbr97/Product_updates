"""Request and response models for groups, variants, and the comparison grid.

The comparison response deliberately mirrors the CLI's grid rather than flattening it into
a price list. A client that receives ``{"flipkart": 82900, "croma": null}`` has been told
that Croma has no price, which is a claim we cannot support -- Croma refused the request
and told us nothing at all. Every cell therefore carries its own status, and the status is
required, not optional.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field

from ...domain.enums import Availability, CellStatus


class GroupCreate(BaseModel):
    """Request body for creating a product group."""

    name: str = Field(min_length=1, max_length=200, examples=["iPhone 17"])
    slug: str | None = Field(
        default=None,
        max_length=64,
        pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$",
        description="URL-safe id. Derived from the name when omitted.",
        examples=["iphone-17"],
    )
    brand: str | None = Field(default=None, max_length=100, examples=["Apple"])
    notes: str | None = None


class VariantAttach(BaseModel):
    """Request body for attaching a tracked listing to a model."""

    product_id: int = Field(ge=1)
    variant: str | None = Field(
        default=None,
        max_length=120,
        description="Model label. Inferred from the listing when omitted.",
        examples=["256GB / Black"],
    )
    attributes: dict[str, str] | None = Field(
        default=None,
        description="Structured model attributes, e.g. {'storage': '256GB'}.",
    )


class VariantSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    label: str
    attributes: dict[str, str] = Field(default_factory=dict)


class GroupResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    slug: str
    name: str
    brand: str | None = None
    notes: str | None = None
    variants: list[VariantSummary] = Field(default_factory=list)
    created_at: datetime


class ComparisonCellResponse(BaseModel):
    """One (model, shop) square.

    ``status`` is the field to branch on, never ``price is None``: several very different
    situations produce a null price, and only one of them says anything about the product.
    """

    status: CellStatus
    price: Decimal | None = None
    currency: str | None = None
    availability: Availability = Availability.UNKNOWN
    product_id: int | None = None
    url: str | None = None
    last_checked_at: datetime | None = None
    is_stale: bool = False
    previous_price: Decimal | None = None


class ComparisonRowResponse(BaseModel):
    variant_id: int | None
    label: str
    attributes: dict[str, str] = Field(default_factory=dict)
    cells: dict[str, ComparisonCellResponse]
    best_price: Decimal | None = None
    #: Every shop at the best price. More than one is a genuine tie, not a bug.
    best_stores: list[str] = Field(default_factory=list)
    spread: Decimal | None = None


class StoreColumn(BaseModel):
    slug: str
    name: str


class ComparisonResponse(BaseModel):
    group_slug: str
    group_name: str
    brand: str | None = None
    stores: list[StoreColumn]
    rows: list[ComparisonRowResponse]
    generated_at: datetime
    currencies: list[str] = Field(default_factory=list)
    #: True when listings report different currencies. Prices are never converted, so a
    #: single "best price" would be meaningless and is omitted rather than guessed.
    mixed_currency: bool = False
