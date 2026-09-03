"""Store catalogue integrity.

The catalogue is data that the seed migration and the CLI both rely on, so its shape is
worth pinning: unique slugs, exactly one fallback, no overlapping domains.
"""

from __future__ import annotations

import pytest

from product_tracker.stores.catalogue import (
    GENERIC_SLUG,
    KNOWN_STORES,
    STORES_BY_SLUG,
    get_store_info,
    resolve_store,
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


class TestStoreResolution:
    """Store identity is by domain and is separate from adapter selection."""

    @pytest.mark.parametrize(
        ("url", "expected"),
        [
            ("https://www.flipkart.com/x/p/itm1", "flipkart"),
            ("https://flipkart.com/x/p/itm1", "flipkart"),
            ("https://www.vijaysales.com/p/P1/2/x", "vijay-sales"),
            ("https://www.reliancedigital.in/product/x-123", "reliance-digital"),
            ("https://www.bigbasket.com/pd/1/x/", "bigbasket"),
            ("https://www.croma.com/x/p/317396", "croma"),
            ("https://blinkit.com/prn/x/prid/12345", "blinkit"),
            ("https://some-other-shop.example.com/p/1", "generic"),
        ],
    )
    def test_resolves_by_domain(self, url: str, expected: str) -> None:
        assert resolve_store(url).slug == expected

    def test_several_named_stores_share_the_generic_adapter(self) -> None:
        """The point of separating store identity from adapter selection."""
        slugs = {"vijay-sales", "reliance-digital", "bigbasket", "croma"}
        for slug in slugs:
            store = STORES_BY_SLUG[slug]
            assert store.adapter_key == "generic"
            assert not store.is_fallback

    def test_a_lookalike_domain_is_not_claimed(self) -> None:
        assert resolve_store("https://notcroma.com/p/1").slug == "generic"

    def test_a_urlless_string_falls_back(self) -> None:
        assert resolve_store("not-a-url").slug == "generic"

    def test_every_adapter_key_has_an_adapter(self) -> None:
        """A store naming an adapter that does not exist would be unfetchable."""
        from product_tracker.stores.registry import StoreRegistry

        available = {adapter.slug for adapter in StoreRegistry().adapters}
        for store in KNOWN_STORES:
            assert store.adapter_key in available, store.slug
