"""Alert (tracking rule) endpoints, and pause/resume."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Query, status

from ...domain.enums import TrackingStatus
from ...services.alert_service import AlertService
from ..deps import DbSession, PageParams
from ..schemas.alerts import AlertCreate, AlertResponse
from ..schemas.common import ErrorResponse, Page
from ..schemas.products import ProductResponse

router = APIRouter(tags=["alerts"])

_NOT_FOUND: dict[int | str, dict[str, Any]] = {
    404: {"model": ErrorResponse, "description": "No such product or alert."}
}


@router.post(
    "/alerts",
    response_model=AlertResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a tracking rule",
    responses={
        **_NOT_FOUND,
        409: {"model": ErrorResponse, "description": "This product already has that rule."},
        422: {"model": ErrorResponse, "description": "Invalid rule parameters."},
    },
)
def create_alert(payload: AlertCreate, session: DbSession) -> AlertResponse:
    rule = AlertService(session).add(
        payload.product_id,
        payload.rule_type,
        params=payload.params_dict(),
        notify_provider=payload.notify_provider,
        cooldown_seconds=payload.cooldown_seconds,
    )
    session.flush()
    return AlertResponse.model_validate(rule)


@router.get("/alerts", response_model=Page[AlertResponse], summary="List tracking rules")
def list_alerts(
    session: DbSession,
    page: PageParams,
    product_id: Annotated[int | None, Query(description="Filter to one product.")] = None,
) -> Page[AlertResponse]:
    result = AlertService(session).list(
        product_id=product_id, limit=page.limit, offset=page.offset
    )
    return Page[AlertResponse](
        items=[AlertResponse.model_validate(rule) for rule in result.items],
        total=result.total,
        limit=result.limit,
        offset=result.offset,
    )


@router.get(
    "/alerts/{rule_id}",
    response_model=AlertResponse,
    summary="Get one tracking rule",
    responses=_NOT_FOUND,
)
def get_alert(rule_id: int, session: DbSession) -> AlertResponse:
    return AlertResponse.model_validate(AlertService(session).get(rule_id))


@router.delete(
    "/alerts/{rule_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a tracking rule",
    responses=_NOT_FOUND,
)
def delete_alert(rule_id: int, session: DbSession) -> None:
    AlertService(session).remove(rule_id)


@router.post(
    "/products/{product_id}/pause",
    response_model=ProductResponse,
    summary="Pause scheduled checks",
    responses=_NOT_FOUND,
)
def pause_product(product_id: int, session: DbSession) -> ProductResponse:
    """The product and its history are kept; only scheduled checks stop.

    A manual check still runs, so a paused product can be tested.
    """
    product = AlertService(session).set_tracking_status(product_id, TrackingStatus.PAUSED)
    return ProductResponse.model_validate(product)


@router.post(
    "/products/{product_id}/resume",
    response_model=ProductResponse,
    summary="Resume scheduled checks",
    responses=_NOT_FOUND,
)
def resume_product(product_id: int, session: DbSession) -> ProductResponse:
    product = AlertService(session).set_tracking_status(product_id, TrackingStatus.ACTIVE)
    return ProductResponse.model_validate(product)
