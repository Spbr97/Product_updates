"""Group endpoints, and the comparison grid.

``GET /groups/{slug}/compare`` is the endpoint this feature exists for. It returns the same
structure the CLI renders: models down the side, shops across the top, and a status on
every cell so a client can tell "blocked" from "sold out" instead of seeing two nulls.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Query, status

from ...domain.errors import NotFoundError
from ...repositories.groups import GroupRepository
from ...services import group_service
from ...services.comparison import DEFAULT_STALE_AFTER, GroupNotFoundError, build_matrix
from ..deps import CurrentReader, CurrentUser, DbSession, RequireWrite
from ..schemas.common import ErrorResponse
from ..schemas.groups import (
    ComparisonCellResponse,
    ComparisonResponse,
    ComparisonRowResponse,
    GroupCreate,
    GroupResponse,
    StoreColumn,
    VariantAttach,
    VariantSummary,
)

router = APIRouter(prefix="/groups", tags=["groups"])

_NOT_FOUND: dict[int | str, dict[str, Any]] = {
    404: {"model": ErrorResponse, "description": "No such group."}
}

_DEFAULT_STALE_HOURS = int(DEFAULT_STALE_AFTER.total_seconds() // 3600)


@router.get("", response_model=list[GroupResponse], summary="List product groups")
def list_groups(session: DbSession, user: CurrentReader) -> list[GroupResponse]:
    groups = GroupRepository(session).list_all(user.id)
    return [GroupResponse.model_validate(group) for group in groups]


@router.post(
    "",
    response_model=GroupResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a product group",
    dependencies=[RequireWrite],
    responses={
        409: {"model": ErrorResponse, "description": "That slug is taken."},
        422: {"model": ErrorResponse, "description": "Invalid name or slug."},
    },
)
def create_group(
    payload: GroupCreate, session: DbSession, user: CurrentUser
) -> GroupResponse:
    group = group_service.create_group(
        session,
        user_id=user.id,
        slug=payload.slug,
        name=payload.name,
        brand=payload.brand,
        notes=payload.notes,
    )
    session.flush()
    return GroupResponse.model_validate(group)


@router.get(
    "/{slug}",
    response_model=GroupResponse,
    summary="Show one product group",
    responses=_NOT_FOUND,
)
def get_group(slug: str, session: DbSession, user: CurrentReader) -> GroupResponse:
    return GroupResponse.model_validate(group_service.get_group(session, user.id, slug))


@router.delete(
    "/{slug}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a product group",
    dependencies=[RequireWrite],
    responses=_NOT_FOUND,
)
def delete_group(slug: str, session: DbSession, user: CurrentUser) -> None:
    """Remove a group. Tracked listings and their price history are kept."""
    group_service.delete_group(session, user.id, slug)


@router.post(
    "/{slug}/listings",
    response_model=VariantSummary,
    status_code=status.HTTP_201_CREATED,
    summary="Attach a tracked listing to a model",
    dependencies=[RequireWrite],
    responses={
        **_NOT_FOUND,
        422: {
            "model": ErrorResponse,
            "description": "No model could be inferred; supply one explicitly.",
        },
    },
)
def attach_listing(
    slug: str, payload: VariantAttach, session: DbSession, user: CurrentUser
) -> VariantSummary:
    _product, variant = group_service.attach_product(
        session,
        payload.product_id,
        user_id=user.id,
        group_slug=slug,
        label=payload.variant,
        attributes=payload.attributes,
    )
    session.flush()
    return VariantSummary.model_validate(variant)


@router.delete(
    "/{slug}/listings/{product_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Detach a listing from its group",
    dependencies=[RequireWrite],
    responses=_NOT_FOUND,
)
def detach_listing(
    slug: str, product_id: int, session: DbSession, user: CurrentUser
) -> None:
    """The listing keeps tracking and keeps its history; it only loses the grouping."""
    # 404 for a group this user does not own, rather than a silent no-op.
    group_service.get_group(session, user.id, slug)
    group_service.detach_product(session, product_id, user.id)


@router.get(
    "/{slug}/compare",
    response_model=ComparisonResponse,
    summary="Compare a product across its models and shops",
    responses=_NOT_FOUND,
)
def compare(
    slug: str,
    session: DbSession,
    user: CurrentReader,
    stale_hours: Annotated[
        int, Query(ge=1, le=8760, description="Flag prices older than this.")
    ] = _DEFAULT_STALE_HOURS,
) -> ComparisonResponse:
    try:
        matrix = build_matrix(
            session, slug, user_id=user.id, stale_after=timedelta(hours=stale_hours)
        )
    except GroupNotFoundError as exc:
        # Translated so the API answers with the project's standard 404 envelope.
        raise NotFoundError("product group", slug) from exc

    return ComparisonResponse(
        group_slug=matrix.group_slug,
        group_name=matrix.group_name,
        brand=matrix.brand,
        stores=[
            StoreColumn(slug=s, name=matrix.store_names[s]) for s in matrix.store_slugs
        ],
        rows=[
            ComparisonRowResponse(
                variant_id=row.variant_id,
                label=row.label,
                attributes=row.attributes,
                cells={
                    slug_: ComparisonCellResponse(
                        status=cell.status,
                        # Every price we hold, including a sold-out listing's last one.
                        # The CLI hides that number because in a grid it reads as an offer;
                        # here the client has ``status`` to disambiguate it, and an API
                        # that drops data it holds is lossy for no benefit.
                        price=cell.price,
                        currency=cell.currency,
                        availability=cell.availability,
                        product_id=cell.product_id,
                        url=cell.url,
                        last_checked_at=cell.last_checked_at,
                        is_stale=cell.is_stale,
                        previous_price=cell.previous_price,
                    )
                    for slug_, cell in row.cells.items()
                },
                best_price=row.best_price,
                best_stores=list(row.best_store_slugs),
                spread=row.spread,
            )
            for row in matrix.rows
        ],
        generated_at=matrix.generated_at,
        currencies=list(matrix.currencies),
        mixed_currency=matrix.mixed_currency,
    )
