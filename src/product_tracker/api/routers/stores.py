"""Store endpoints.

Lists the retailers this build recognises, from the catalogue. The catalogue is store
identity; the adapter that reads each one is an implementation detail, exposed as
``adapter`` for diagnosis.
"""

from __future__ import annotations

from fastapi import APIRouter

from ...stores.catalogue import KNOWN_STORES
from ..schemas.products import StoreResponse

router = APIRouter(prefix="/stores", tags=["stores"])


@router.get("", response_model=list[StoreResponse], summary="List supported stores")
def list_stores() -> list[StoreResponse]:
    return [
        StoreResponse(
            slug=info.slug,
            name=info.display_name,
            domains=list(info.domains),
            adapter=info.adapter_key,
            is_fallback=info.is_fallback,
        )
        for info in KNOWN_STORES
    ]
