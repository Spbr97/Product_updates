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
from ...stores.registry import default_registry
from ..deps import Config, DbSession, PageParams, RequireWrite
from ..schemas.common import ErrorResponse, Page
from ..schemas.products import CheckResponse, ProductCreate, ProductResponse

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
    payload: ProductCreate, session: DbSession, settings: Config
) -> ProductResponse:
    service = ProductService(session, default_registry(), settings)
    product = service.add(payload.url, check_interval_seconds=payload.check_interval_seconds)
    session.flush()
    return ProductResponse.model_validate(product)


@router.get("", response_model=Page[ProductResponse], summary="List tracked products")
def list_products(
    session: DbSession,
    settings: Config,
    page: PageParams,
    store: Annotated[str | None, Query(description="Filter by store slug.")] = None,
    tracking_status: Annotated[
        TrackingStatus | None, Query(description="Filter by tracking status.")
    ] = None,
) -> Page[ProductResponse]:
    service = ProductService(session, default_registry(), settings)
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
def get_product(product_id: int, session: DbSession, settings: Config) -> ProductResponse:
    service = ProductService(session, default_registry(), settings)
    return ProductResponse.model_validate(service.get(product_id))


@router.delete(
    "/{product_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Stop tracking a product",
    dependencies=[RequireWrite],
    responses={404: {"model": ErrorResponse, "description": "No such product."}},
)
def delete_product(product_id: int, session: DbSession, settings: Config) -> None:
    """Delete the product. Its history, rules, and executions cascade with it."""
    ProductService(session, default_registry(), settings).remove(product_id)


@router.post(
    "/{product_id}/check",
    response_model=CheckResponse,
    summary="Check a product now",
    dependencies=[RequireWrite],
    responses={404: {"model": ErrorResponse, "description": "No such product."}},
)
def check_product(product_id: int, session: DbSession, settings: Config) -> CheckResponse:
    """Run a check immediately.

    Returns 200 with the execution record even when the store could not be read: a failed
    check is a successfully recorded fact, and the caller inspects ``status`` and
    ``error_type``. Returning 502 here would conflate "we could not reach the store" with
    "this API is broken".
    """
    # Runs in its own transactions, so notification delivery never holds this request's
    # database session open while an SMTP server or webhook takes its time.
    outcome = run_check(product_id, settings=settings, registry=default_registry())
    execution = session.get(CheckExecution, outcome.execution_id)
    return CheckResponse.model_validate(execution)
