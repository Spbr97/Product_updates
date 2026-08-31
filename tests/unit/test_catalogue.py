"""Store catalogue integrity.

The catalogue is data that the seed migration and the CLI both rely on, so its shape is
worth pinning: unique slugs, exactly one fallback, no overlapping domains.
"""

from __future__ import annotations

from product_tracker.stores.catalogue import (
    GENERIC_SLUG,
    KNOWN_STORES,
    STORES_BY_SLUG,
    get_store_info,
)


class TestCatalogue:
    def test_slugs_are_unique(self) -> None:
        slugs = [store.slug for store in KNOWN_STORES]
        assert len(slugs) == len(set(slugs))

    def test_exactly_one_fallback(self) -> None:
        """Resolution needs a single deterministic last resort."""
        fallbacks = [store for store in KNOWN_STORES if store.is_fallback]
        assert len(fallbacks) == 1
        assert fallbacks[0].slug == GENERIC_SLUG

    def test_the_fallback_claims_no_domains(self) -> None:
        """It matches by being last, not by hostname."""
        assert STORES_BY_SLUG[GENERIC_SLUG].domains == ()

    def test_named_stores_declare_domains(self) -> None:
        for store in KNOWN_STORES:
            if not store.is_fallback:
                assert store.domains, f"{store.slug} must declare at least one domain"

    def test_domains_are_not_shared_between_stores(self) -> None:
        seen: dict[str, str] = {}
        for store in KNOWN_STORES:
            for domain in store.domains:
                assert domain not in seen, f"{domain} claimed by {seen[domain]} and {store.slug}"
                seen[domain] = store.slug

    def test_lookup_by_slug(self) -> None:
        assert get_store_info("flipkart") is not None
        assert get_store_info("does-not-exist") is None
