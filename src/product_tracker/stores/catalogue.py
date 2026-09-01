"""The static catalogue of supported stores.

**A store is not an adapter.** A store is a retailer, identified by its domains. An adapter
is the code that reads its pages. Several stores share the generic adapter -- Vijay Sales,
BigBasket, and Reliance Digital all publish enough structured data for it -- and they are
still distinct stores. Conflating the two would have every one of them display as
"generic", make per-store filtering useless, and lump their statistics together.

So this module owns *store identity* (domain -> store) and the registry owns *adapter
selection*. A store's ``adapter_key`` says which adapter reads it.

Adding a store that the generic adapter already handles is one entry here plus a seed
migration. Only add an adapter when the generic one cannot read the site.
"""

from __future__ import annotations

from ..domain.models import StoreInfo
from ..utils.urls import host_of

#: Fallback slug. Used for any site with no catalogue entry; the generic adapter reads it.
GENERIC_SLUG = "generic"

KNOWN_STORES: tuple[StoreInfo, ...] = (
    StoreInfo(
        slug="flipkart",
        display_name="Flipkart",
        domains=("flipkart.com", "dl.flipkart.com"),
        adapter_key="flipkart",
    ),
    # Verified 2026-09-01: these three are read by the generic adapter over plain HTTP.
    StoreInfo(
        slug="vijay-sales",
        display_name="Vijay Sales",
        domains=("vijaysales.com",),
        adapter_key="generic",
    ),
    StoreInfo(
        slug="reliance-digital",
        display_name="Reliance Digital",
        domains=("reliancedigital.in",),
        adapter_key="generic",
    ),
    StoreInfo(
        slug="bigbasket",
        display_name="BigBasket",
        domains=("bigbasket.com",),
        adapter_key="generic",
    ),
    # Listed so its checks are attributed to Croma rather than lost in "generic".
    # Croma blocks automated access at the edge -- a real headless browser gets the same
    # 403 -- so checks are expected to fail and the circuit breaker backs it off.
    StoreInfo(
        slug="croma",
        display_name="Croma",
        domains=("croma.com",),
        adapter_key="generic",
    ),
    # Verified 2026-09-01 against a live Galaxy S25 listing.
    StoreInfo(
        slug="samsung",
        display_name="Samsung",
        domains=("samsung.com",),
        adapter_key="generic",
    ),
    StoreInfo(
        slug="sangeetha",
        display_name="Sangeetha Mobiles",
        domains=("sangeethamobiles.com",),
        adapter_key="generic",
    ),
    # Amazon *product* pages serve us; the homepage answers with an AWS WAF JavaScript
    # challenge. The pages carry no JSON-LD and render their price with JavaScript, so
    # checks land on PRICE_NOT_FOUND: we identify the listing and honestly report that we
    # could not read a price. Listed anyway so those checks are attributed to Amazon
    # instead of disappearing into "generic", and so the per-host circuit breaker treats
    # Amazon's failures as Amazon's. Reading it properly needs an Amazon adapter; the
    # WAF challenge is a security control and is not something to work around.
    StoreInfo(
        slug="amazon-in",
        display_name="Amazon India",
        domains=("amazon.in",),
        adapter_key="generic",
    ),
    StoreInfo(
        slug=GENERIC_SLUG,
        display_name="Other (schema.org)",
        domains=(),
        adapter_key="generic",
        is_fallback=True,
    ),
)

STORES_BY_SLUG: dict[str, StoreInfo] = {store.slug: store for store in KNOWN_STORES}

#: The catch-all, held separately so resolution never has to search for it.
GENERIC_STORE: StoreInfo = STORES_BY_SLUG[GENERIC_SLUG]


def get_store_info(slug: str) -> StoreInfo | None:
    return STORES_BY_SLUG.get(slug)


def resolve_store(url: str) -> StoreInfo:
    """The store a URL belongs to, by hostname.

    Matches the host exactly or as a subdomain, so ``vijaysales.com`` also claims
    ``www.vijaysales.com`` but never ``notvijaysales.com``. Anything unrecognised is the
    generic store -- still trackable, just not named.
    """
    host = host_of(url)
    if not host:
        return GENERIC_STORE

    for store in KNOWN_STORES:
        if store.is_fallback:
            continue
        if any(host == domain or host.endswith(f".{domain}") for domain in store.domains):
            return store
    return GENERIC_STORE
