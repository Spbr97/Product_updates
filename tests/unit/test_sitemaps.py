"""Discovery through the catalogues retailers publish.

This is the route for shops whose search we must not crawl (their robots.txt says so) or
cannot read (their results never reach the DOM). A sitemap is published to be read, which
makes it both the most polite option and, for those shops, the only one that works.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from product_tracker.domain.enums import FetchOutcome, SearchOutcome
from product_tracker.domain.models import FetchContext
from product_tracker.stores import sitemaps
from product_tracker.stores.http import FetchFailure, FetchSuccess
from product_tracker.stores.search import SearchConfig, SitemapSearch, score_title

CTX = FetchContext(user_agent="product-tracker-test", verify_public_host=False)
PRODUCT_PATTERN = r"/p/[A-Za-z]?[0-9]+/"

INDEX = """<?xml version="1.0"?>
<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <sitemap><loc>https://shop.test/brands-sitemap.xml</loc></sitemap>
  <sitemap><loc>https://shop.test/products-sitemap.xml</loc></sitemap>
</sitemapindex>"""

PRODUCTS = """<?xml version="1.0"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://shop.test/p/P1/11/samsung-galaxy-s25-5g-12gb-ram-256gb-storage-navy</loc></url>
  <url><loc>https://shop.test/p/P2/12/samsung-galaxy-s25-fe-8gb-256gb-navy</loc></url>
  <url><loc>https://shop.test/p/13/samsung-galaxy-s25-silicone-case-mint</loc></url>
  <url><loc>https://shop.test/c/mobiles</loc></url>
</urlset>"""

BRANDS = """<?xml version="1.0"?>
<urlset><url><loc>https://shop.test/brand/samsung</loc></url></urlset>"""


class Fetcher:
    """Serves canned sitemaps and records what was asked for."""

    def __init__(self, pages: dict[str, str]) -> None:
        self.pages = pages
        self.calls: list[str] = []

    def __call__(self, url: str, ctx: FetchContext) -> FetchSuccess | FetchFailure:
        self.calls.append(url)
        body = self.pages.get(url)
        if body is None:
            return FetchFailure(FetchOutcome.HTTP_ERROR, "missing", http_status=404)
        return FetchSuccess(html=body, url=url, http_status=200)


@pytest.fixture(autouse=True)
def _clean() -> Iterator[None]:
    sitemaps.clear_cache()
    yield
    sitemaps.clear_cache()


@pytest.fixture
def fetcher(monkeypatch: pytest.MonkeyPatch) -> Fetcher:
    stub = Fetcher(
        {
            "https://shop.test/robots.txt": "Sitemap: https://shop.test/sitemap-index.xml",
            "https://shop.test/sitemap-index.xml": INDEX,
            "https://shop.test/products-sitemap.xml": PRODUCTS,
            "https://shop.test/brands-sitemap.xml": BRANDS,
        }
    )
    monkeypatch.setattr(sitemaps, "http_fetch", stub)
    return stub


SPEC = sitemaps.SitemapSpec(include="products-sitemap")


def collect(slug: str = "shop") -> tuple[str, ...]:
    return sitemaps.product_urls(slug, SPEC, PRODUCT_PATTERN, "https://shop.test/", CTX)


class TestCollecting:
    def test_finds_the_advertised_catalogue(self, fetcher: Fetcher) -> None:
        urls = collect()
        assert len(urls) == 3
        assert all("/p/" in url for url in urls)

    def test_non_product_urls_are_excluded(self, fetcher: Fetcher) -> None:
        assert not any("/c/mobiles" in url for url in collect())

    def test_only_the_included_children_are_fetched(self, fetcher: Fetcher) -> None:
        """A shop advertising brand, category and blog sitemaps should cost one fetch."""
        collect()
        assert "https://shop.test/brands-sitemap.xml" not in fetcher.calls

    def test_the_catalogue_is_cached(self, fetcher: Fetcher) -> None:
        """A retailer's whole catalogue is not something to re-pull once per query."""
        collect()
        before = len(fetcher.calls)
        collect()
        assert len(fetcher.calls) == before

    def test_an_unreadable_sitemap_yields_nothing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Which the caller must report as "could not look", never as "not stocked"."""
        monkeypatch.setattr(sitemaps, "http_fetch", Fetcher({}))
        assert collect("missing") == ()


class TestSlugReading:
    @pytest.mark.parametrize(
        ("url", "expected"),
        [
            ("https://shop.test/p/P1/11/samsung-galaxy-s25-navy", "samsung galaxy s25 navy"),
            ("https://shop.test/product/lg-fridge-abc", "lg fridge abc"),
        ],
    )
    def test_reads_the_descriptive_segment(self, url: str, expected: str) -> None:
        """Retailers put the model, capacity and colour in the slug.

        That is exactly what a query is trying to match, which is why a catalogue of bare
        URLs is searchable at all.
        """
        assert sitemaps.slug_words(url) == expected

    def test_capacities_are_upper_cased(self) -> None:
        """256Gb reads as a typo where 256GB reads as a size."""
        title = sitemaps.title_from_slug("https://shop.test/p/P1/1/galaxy-s25-256gb-navy")
        assert "256GB" in title
        assert "Navy" in title


class TestDisqualifyingWords:
    def test_a_case_is_not_the_phone(self) -> None:
        """The failure that made this necessary.

        A sitemap has no relevance ranking of its own, so a search for a phone came back as
        five silicone cases and a screen protector -- every one of them a perfect match on
        every word asked for.
        """
        _score, qualifiers = score_title("Galaxy S25", "Samsung Galaxy S25 Silicone Case Mint")
        assert "case" in qualifiers

    def test_a_screen_protector_is_not_the_phone(self) -> None:
        _score, qualifiers = score_title(
            "Galaxy S25", "Samsung Galaxy S25 Anti Reflecting Film Transparent"
        )
        assert "film" in qualifiers

    def test_a_phone_describing_its_own_display_is_not_an_accessory(self) -> None:
        """Regression.

        "display" and "unit" were on the list, and every real phone whose title mentions
        its screen came back flagged as an accessory.
        """
        _score, qualifiers = score_title(
            "Galaxy S25", "Samsung Galaxy S25 5G with 6.2 inch FHD+ Display Unit Navy"
        )
        assert qualifiers == ()

    def test_the_phone_itself_is_unqualified(self) -> None:
        score, qualifiers = score_title(
            "Galaxy S25", "Samsung Galaxy S25 5G 12GB RAM 256GB Storage Navy"
        )
        assert score == 1.0
        assert qualifiers == ()


class TestSitemapSearch:
    @pytest.fixture
    def configured(self, monkeypatch: pytest.MonkeyPatch, fetcher: Fetcher) -> None:
        import product_tracker.stores.search as search_module

        monkeypatch.setattr(
            search_module,
            "load_search_config",
            lambda slug: SearchConfig(
                url_template="https://shop.test/search?q={query}",
                result_link=(),
                product_url_pattern=PRODUCT_PATTERN,
                sitemap="auto",
                sitemap_include="products-sitemap",
            ),
        )

    def test_finds_the_product(self, configured: None) -> None:
        result = SitemapSearch("shop").search("Galaxy S25", CTX, limit=5)

        assert result.outcome is SearchOutcome.OK
        assert result.hits
        assert result.hits[0].is_exact

    def test_hits_say_where_they_came_from(self, configured: None) -> None:
        """Their name was read off the URL; the shop never published it, and there is no
        price. Presenting a derived name as the retailer's own would be a small lie."""
        hits = SitemapSearch("shop").search("Galaxy S25", CTX).hits

        assert hits
        for hit in hits:
            assert hit.from_sitemap
            assert hit.price is None

    def test_a_different_model_ranks_below_the_real_one(self, configured: None) -> None:
        hits = SitemapSearch("shop").search("Galaxy S25", CTX, limit=5).hits

        assert hits[0].qualifiers == ()
        assert any("fe" in hit.qualifiers for hit in hits)

    def test_nothing_matching_is_no_results_not_a_failure(self, configured: None) -> None:
        result = SitemapSearch("shop").search("Miele dishwasher", CTX)
        assert result.outcome is SearchOutcome.NO_RESULTS

    def test_a_store_without_a_catalogue_says_so(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import product_tracker.stores.search as search_module

        monkeypatch.setattr(
            search_module,
            "load_search_config",
            lambda slug: SearchConfig(
                url_template="https://shop.test/search?q={query}",
                result_link=(),
                product_url_pattern=PRODUCT_PATTERN,
                sitemap="none",
            ),
        )
        result = SitemapSearch("shop").search("Galaxy S25", CTX)
        assert result.outcome is SearchOutcome.UNSUPPORTED


class TestPathFurnitureInSlugs:
    """Samsung ends every product URL with ``/buy/``.

    Without skipping it, the readable part of ``/in/smartphones/galaxy-s25/buy/`` is the
    word "buy", every Samsung product scores zero against every query, and the store looks
    like it stocks nothing.
    """

    def test_a_trailing_buy_segment_is_skipped(self) -> None:
        from product_tracker.stores.sitemaps import slug_words

        assert slug_words("https://www.samsung.com/in/smartphones/galaxy-s25/buy/") == (
            "galaxy s25"
        )

    def test_the_derived_title_reads_as_a_product(self) -> None:
        from product_tracker.stores.sitemaps import title_from_slug

        assert (
            title_from_slug("https://www.samsung.com/in/audio-sound/galaxy-buds4-pro/buy/")
            == "Galaxy Buds4 Pro"
        )

    def test_ordinary_slugs_are_unchanged(self) -> None:
        """The stores that already worked must keep working."""
        from product_tracker.stores.sitemaps import slug_words

        assert slug_words(
            "https://www.vijaysales.com/p/P237290/237287/"
            "samsung-galaxy-s25-5g-12gb-ram-256gb-storage-icyblue"
        ) == "samsung galaxy s25 5g 12gb ram 256gb storage icyblue"
