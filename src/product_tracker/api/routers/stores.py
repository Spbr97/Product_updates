"""Store endpoints.

Reports the adapters compiled into this build rather than the ``stores`` table: the
registry is the source of truth for what can actually be fetched, and a database row
without an adapter would be misleading.
"""

from __future__ import annotations

from fastapi import APIRouter

from ...stores.registry import default_registry
from ..schemas.products import StoreResponse

router = APIRouter(prefix="/stores", tags=["stores"])


@router.get("", response_model=list[StoreResponse], summary="List supported stores")
def list_stores() -> list[StoreResponse]:
    return [
        StoreResponse(
            slug=info.slug,
            name=info.display_name,
            domains=list(info.domains),
            is_fallback=info.is_fallback,
        )
        for info in default_registry().list_stores()
    ]
