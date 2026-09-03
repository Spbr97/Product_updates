"""Alert (tracking rule) endpoints, and pause/resume."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Query, status

from ...domain.enums import TrackingStatus
from ...services.alert_service import AlertService
from ..deps import CurrentReader, CurrentUser, DbSession, PageParams, RequireWrite
from ..schemas.alerts import AlertCreate, AlertResponse, AlertUpdate
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
    dependencies=[RequireWrite],
    responses={
        **_NOT_FOUND,
        409: {"model": ErrorResponse, "description": "This product already has that rule."},
        422: {"model": ErrorResponse, "description": "Invalid rule parameters."},
    },
)
def create_alert(
    payload: AlertCreate, session: DbSession, user: CurrentUser
) -> AlertResponse:
    rule = AlertService(session, user.id).add(
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
    user: CurrentReader,
    page: PageParams,
    product_id: Annotated[int | None, Query(description="Filter to one product.")] = None,
) -> Page[AlertResponse]:
    result = AlertService(session, user.id).list(
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
def get_alert(rule_id: int, session: DbSession, user: CurrentReader) -> AlertResponse:
    return AlertResponse.model_validate(AlertService(session, user.id).get(rule_id))


@router.patch(
    "/alerts/{rule_id}",
    response_model=AlertResponse,
    summary="Change a rule's cooldown, or turn it on and off",
    dependencies=[RequireWrite],
    responses={
        **_NOT_FOUND,
        422: {"model": ErrorResponse, "description": "Invalid cooldown."},
    },
)
def update_alert(
    rule_id: int, payload: AlertUpdate, session: DbSession, user: CurrentUser
) -> AlertResponse:
    """Apply whichever of ``cooldown_seconds`` and ``enabled`` the body actually carries.

    An empty body is a no-op that returns the rule unchanged; an unknown ``rule_id`` is
    404 whether or not any field was sent.
    """
    service = AlertService(session, user.id)
    fields = payload.model_fields_set
    rule = service.get(rule_id)
    if "cooldown_seconds" in fields:
        rule = service.set_cooldown(rule_id, payload.cooldown_seconds)
    if "enabled" in fields and payload.enabled is not None:
        rule = service.set_enabled(rule_id, payload.enabled)
    session.flush()
    return AlertResponse.model_validate(rule)


@router.delete(
    "/alerts/{rule_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a tracking rule",
    dependencies=[RequireWrite],
    responses=_NOT_FOUND,
)
def delete_alert(rule_id: int, session: DbSession, user: CurrentUser) -> None:
    AlertService(session, user.id).remove(rule_id)


@router.post(
    "/products/{product_id}/pause",
    response_model=ProductResponse,
    summary="Pause scheduled checks",
    dependencies=[RequireWrite],
    responses=_NOT_FOUND,
)
def pause_product(
    product_id: int, session: DbSession, user: CurrentUser
) -> ProductResponse:
    """The product and its history are kept; only scheduled checks stop.

    A manual check still runs, so a paused product can be tested.
    """
    product = AlertService(session, user.id).set_tracking_status(product_id, TrackingStatus.PAUSED)
    return ProductResponse.model_validate(product)


@router.post(
    "/products/{product_id}/resume",
    response_model=ProductResponse,
    summary="Resume scheduled checks",
    dependencies=[RequireWrite],
    responses=_NOT_FOUND,
)
def resume_product(
    product_id: int, session: DbSession, user: CurrentUser
) -> ProductResponse:
    product = AlertService(session, user.id).set_tracking_status(product_id, TrackingStatus.ACTIVE)
    return ProductResponse.model_validate(product)
