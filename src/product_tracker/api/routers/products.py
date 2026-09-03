"""Product endpoints.

Routers stay thin: they translate HTTP to service calls and back. Domain exceptions are
raised freely -- the handlers registered in ``api/errors.py`` map them to status codes, so
no router repeats that mapping.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query, status

from ...db.models import CheckExecution
from ...domain.enums import TrackingStatus
from ...services.check_runner import run_check
from ...services.product_service import ProductService
from ...services.user_service import assert_subscribed
from ...stores.registry import default_registry
from ..deps import Config, CurrentReader, CurrentUser, DbSession, PageParams, RequireWrite
from ..schemas.common import ErrorResponse, Page
from ..schemas.products import (
    CheckResponse,
    ProductCreate,
    ProductResponse,
    ProductUpdate,
)

router = APIRouter(prefix="/products", tags=["products"])


@router.post(
    "",
    response_model=ProductResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Track a new product",
    dependencies=[RequireWrite],
    responses={
        409: {"model": ErrorResponse, "description": "This listing is already tracked."},
        422: {"model": ErrorResponse, "description": "URL rejected by validation or SSRF guard."},
    },
)
def create_product(
    payload: ProductCreate, session: DbSession, settings: Config, user: CurrentUser
) -> ProductResponse:
    service = ProductService(session, default_registry(), settings, user.id)
    product = service.add(payload.url, check_interval_seconds=payload.check_interval_seconds)
    session.flush()
    return ProductResponse.model_validate(product)


@router.get("", response_model=Page[ProductResponse], summary="List tracked products")
def list_products(
    session: DbSession,
    settings: Config,
    user: CurrentReader,
    page: PageParams,
    store: Annotated[str | None, Query(description="Filter by store slug.")] = None,
    tracking_status: Annotated[
        TrackingStatus | None, Query(description="Filter by tracking status.")
    ] = None,
) -> Page[ProductResponse]:
    service = ProductService(session, default_registry(), settings, user.id)
    result = service.list(
        limit=page.limit, offset=page.offset, store_slug=store, tracking_status=tracking_status
    )
    return Page[ProductResponse](
        items=[ProductResponse.model_validate(p) for p in result.items],
        total=result.total,
        limit=result.limit,
        offset=result.offset,
    )


@router.get(
    "/{product_id}",
    response_model=ProductResponse,
    summary="Get one product",
    responses={404: {"model": ErrorResponse, "description": "No such product."}},
)
def get_product(
    product_id: int, session: DbSession, settings: Config, user: CurrentReader
) -> ProductResponse:
    service = ProductService(session, default_registry(), settings, user.id)
    return ProductResponse.model_validate(service.get(product_id))


@router.patch(
    "/{product_id}",
    response_model=ProductResponse,
    summary="Change a product's check interval",
    dependencies=[RequireWrite],
    responses={
        404: {"model": ErrorResponse, "description": "No such product."},
        422: {"model": ErrorResponse, "description": "Interval below the politeness floor."},
    },
)
def update_product(
    product_id: int,
    payload: ProductUpdate,
    session: DbSession,
    settings: Config,
    user: CurrentUser,
) -> ProductResponse:
    """Currently only the check interval, so the CLI's ``set-interval`` has an API twin.

    ``null`` returns the product to the global default. Scoped to the caller's own
    listings; another user's id is 404, matching every other route here.
    """
    service = ProductService(session, default_registry(), settings, user.id)
    product = service.set_check_interval(product_id, payload.check_interval_seconds)
    session.flush()
    return ProductResponse.model_validate(product)


@router.delete(
    "/{product_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Stop tracking a product",
    dependencies=[RequireWrite],
    responses={404: {"model": ErrorResponse, "description": "No such product."}},
)
def delete_product(
    product_id: int, session: DbSession, settings: Config, user: CurrentUser
) -> None:
    """Delete the product. Its history, rules, and executions cascade with it."""
    ProductService(session, default_registry(), settings, user.id).remove(product_id)


@router.post(
    "/{product_id}/check",
    response_model=CheckResponse,
    summary="Check a product now",
    dependencies=[RequireWrite],
    responses={404: {"model": ErrorResponse, "description": "No such product."}},
)
def check_product(
    product_id: int, session: DbSession, settings: Config, user: CurrentUser
) -> CheckResponse:
    """Run a check immediately.

    Returns 200 with the execution record even when the store could not be read: a failed
    check is a successfully recorded fact, and the caller inspects ``status`` and
    ``error_type``. Returning 502 here would conflate "we could not reach the store" with
    "this API is broken".

    Scoped to the caller's own listings. This took no user at all before, which made it two
    things it should never have been: a way to read any listing's current price by id --
    the response carries ``extracted_price`` -- and a way to make this deployment fetch
    from a retailer on demand for a listing the caller does not watch. Listing ids are
    sequential, so both were a matter of counting. Reported as not found rather than
    forbidden, so ids cannot be enumerated by watching which ones come back 403.
    """
    assert_subscribed(session, user.id, product_id)
    # Runs in its own transactions, so notification delivery never holds this request's
    # database session open while an SMTP server or webhook takes its time.
    outcome = run_check(product_id, settings=settings, registry=default_registry())
    execution = session.get(CheckExecution, outcome.execution_id)
    return CheckResponse.model_validate(execution)
