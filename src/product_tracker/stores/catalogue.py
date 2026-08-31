"""The static catalogue of supported stores.

This is deliberately separate from the adapter classes. The catalogue is plain data, so
the seed migration, the CLI, and the API can all read it without importing HTTP clients or
Playwright. Phase 2 binds each ``adapter_key`` to a concrete
:class:`~product_tracker.stores.base.StoreAdapter`.

Adding a store means: add an entry here, add the adapter module, register it, and run
``product-tracker stores sync``.
"""

from __future__ import annotations

from ..domain.models import StoreInfo

#: Fallback slug. The generic adapter accepts any http(s) URL and extracts schema.org
#: JSON-LD / OpenGraph metadata, so an unrecognised site is still trackable.
GENERIC_SLUG = "generic"

KNOWN_STORES: tuple[StoreInfo, ...] = (
    StoreInfo(
        slug="flipkart",
        display_name="Flipkart",
        domains=("flipkart.com", "www.flipkart.com", "dl.flipkart.com"),
        adapter_key="flipkart",
    ),
    StoreInfo(
        slug=GENERIC_SLUG,
        display_name="Generic (schema.org)",
        domains=(),
        adapter_key="generic",
        is_fallback=True,
    ),
)

STORES_BY_SLUG: dict[str, StoreInfo] = {store.slug: store for store in KNOWN_STORES}


def get_store_info(slug: str) -> StoreInfo | None:
    return STORES_BY_SLUG.get(slug)
