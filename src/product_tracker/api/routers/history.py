"""History and statistics endpoints.

Mounted under ``/products/{id}`` because history has no identity of its own -- it is only
ever read for one product.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from ...services.history_service import HistoryService
from ..deps import DbSession, PageParams
from ..schemas.common import ErrorResponse, Page
from ..schemas.history import (
    AvailabilityHistoryEntry,
    PriceHistoryEntry,
    PriceStatsResponse,
)

router = APIRouter(prefix="/products", tags=["history"])

_NOT_FOUND: dict[int | str, dict[str, Any]] = {
    404: {"model": ErrorResponse, "description": "No such product."}
}


@router.get(
    "/{product_id}/history",
    response_model=Page[PriceHistoryEntry],
    summary="Recorded price history",
    responses=_NOT_FOUND,
)
def price_history(
    product_id: int, session: DbSession, page: PageParams
) -> Page[PriceHistoryEntry]:
    """Newest first. Only meaningful observations are stored, so consecutive identical
    prices do not appear twice -- see ``/checks`` semantics in the README."""
    result = HistoryService(session).price_history(
        product_id, limit=page.limit, offset=page.offset
    )
    return Page[PriceHistoryEntry](
        items=[PriceHistoryEntry.model_validate(entry) for entry in result.items],
        total=result.total,
        limit=result.limit,
        offset=result.offset,
    )


@router.get(
    "/{product_id}/availability",
    response_model=Page[AvailabilityHistoryEntry],
    summary="Recorded availability transitions",
    responses=_NOT_FOUND,
)
def availability_history(
    product_id: int, session: DbSession, page: PageParams
) -> Page[AvailabilityHistoryEntry]:
    """One row per transition, not per check."""
    result = HistoryService(session).availability_history(
        product_id, limit=page.limit, offset=page.offset
    )
    return Page[AvailabilityHistoryEntry](
        items=[AvailabilityHistoryEntry.model_validate(entry) for entry in result.items],
        total=result.total,
        limit=result.limit,
        offset=result.offset,
    )


@router.get(
    "/{product_id}/stats",
    response_model=PriceStatsResponse | None,
    summary="Price statistics",
    responses=_NOT_FOUND,
)
def price_stats(product_id: int, session: DbSession) -> PriceStatsResponse | None:
    """Returns ``null`` when the product has no recorded prices yet.

    A product that exists but has never been checked successfully is not an error; the
    caller distinguishes "no data" from "no product" by the 404.
    """
    stats = HistoryService(session).stats(product_id)
    return PriceStatsResponse.model_validate(stats) if stats else None
