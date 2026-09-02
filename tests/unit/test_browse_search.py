"""Finding a product on the category listings a shop publishes for crawlers.

The third discovery route, and the one Flipkart leaves open: its robots.txt disallows
``/search?`` and its product catalogue is roughly 275 million URLs, but it advertises two
sitemaps of browse pages that robots.txt permits and that carry real titles and prices.

No network here. The live behaviour these tests encode was measured first:

* The Galaxy S25 was not on page one of Flipkart's Samsung phone listing -- it was on page
  two, behind older models. Hence paging, and hence stopping the moment a match appears.
* "Galaxy S25" against the *whole* phones listing paged four times and matched nothing,
  because a category page is ordered by popularity. Hence requiring a brand.
"""

from __future__ import annotations

import pytest

from product_tracker.domain.enums import SearchOutcome
from product_tracker.domain.models import FetchContext
from product_tracker.stores import robots, search, sitemaps
from product_tracker.stores.http import FetchSuccess
from product_tracker.stores.search import BrowseSearch

CTX = FetchContext(user_agent="product-tracker-test", verify_public_host=False)

#: What Flipkart advertises, in miniature: brand pages under a category, and other
#: categories for the same brand that must not be picked instead.
BROWSE_URLS = (
    "https://www.flipkart.com/mobiles/samsung~brand/pr?sid=tyy,4io",
    "https://www.flipkart.com/mobiles/apple~brand/pr?sid=tyy,4io",
    "https://www.flipkart.com/tablets/samsung~brand/pr?sid=tyy,hry",
    "https://www.flipkart.com/computers/computer-components/monitors/samsung~brand/pr?sid=6bo",
    "https://www.flipkart.com/mobile-accessories/power-banks/pr?sid=tyy,4mr,fu6",
)


def card(title: str, price: str, item: str) -> str:
    """One result in the shape the Flipkart selectors expect: the card *is* the link."""
    return (
        f'<a href="/{title.replace(" ", "-").lower()}/p/itm{item}">'
        f'<div class="KzDlHZ">{title}</div><div class="Nx9bqj">₹{price}</div></a>'
    )


def page(*cards: str) -> str:
    return f"<html><body>{''.join(cards)}</body></html>"


@pytest.fixture
def index(monkeypatch: pytest.MonkeyPatch) -> None:
    """Serve the browse index without touching a sitemap."""
    monkeypatch.setattr(sitemaps, "product_urls", lambda *a, **k: BROWSE_URLS)
    monkeypatch.setattr(robots, "is_allowed", lambda url, ctx: True)


@pytest.fixture
def fetched(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Record every page requested, so the cost of a search is testable."""
    requested: list[str] = []
    pages = {
        1: page(
            card("Samsung Galaxy S24 5G", "49,999", "aaa"),
            card("Samsung Galaxy M06 5G", "15,460", "bbb"),
        ),
        2: page(
            card("Samsung Galaxy S25 5G (Mint, 256 GB)", "79,999", "ccc"),
            card("Samsung Galaxy S25 FE 5G", "59,999", "ddd"),
        ),
    }

    def fake(url: str, ctx: FetchContext) -> FetchSuccess:
        requested.append(url)
        number = int(url.rsplit("page=", 1)[-1]) if "page=" in url else 1
        return FetchSuccess(html=pages.get(number, page()), url=url, http_status=200)

    monkeypatch.setattr(search, "http_fetch", fake)
    return requested


class TestChoosingTheListing:
    def test_it_picks_the_brand_page_for_the_right_category(
        self, index: None, fetched: list[str]
    ) -> None:
        """A Samsung phone must not be looked for on the Samsung monitor listing."""
        BrowseSearch("flipkart", "phone").search("Samsung Galaxy S25", CTX)

        assert fetched, "no page was requested"
        assert "/mobiles/samsung~brand/" in fetched[0]

    def test_a_query_naming_no_brand_costs_nothing(
        self, index: None, fetched: list[str]
    ) -> None:
        """The expensive failure this avoids: four pages of a popularity-ordered listing."""
        result = BrowseSearch("flipkart", "phone").search("Galaxy S25", CTX)

        assert result.outcome is SearchOutcome.NO_RESULTS
        assert fetched == []
        assert result.message is not None and "brand" in result.message

    def test_an_unreadable_index_is_not_reported_as_no_results(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(sitemaps, "product_urls", lambda *a, **k: ())
        result = BrowseSearch("flipkart", "phone").search("Samsung Galaxy S25", CTX)

        assert result.outcome is not SearchOutcome.OK


class TestPaging:
    def test_it_pages_until_the_model_appears(
        self, index: None, fetched: list[str]
    ) -> None:
        """The measurement this exists for: the S25 was on page two, not page one."""
        result = BrowseSearch("flipkart", "phone").search("Samsung Galaxy S25", CTX)

        assert result.outcome is SearchOutcome.OK
        assert next(h.title for h in result.hits).startswith("Samsung Galaxy S25 5G")
        assert len(fetched) == 2, f"expected two pages, got {fetched}"

    def test_it_stops_as_soon_as_it_has_a_match(
        self, index: None, fetched: list[str]
    ) -> None:
        """Paging past an answer costs the shop requests for products nobody asked about."""
        BrowseSearch("flipkart", "phone").search("Samsung Galaxy S24", CTX)

        assert len(fetched) == 1

    def test_paging_is_bounded(self, index: None, fetched: list[str]) -> None:
        result = BrowseSearch("flipkart", "phone").search("Samsung Galaxy Z Fold 9", CTX)

        assert result.outcome is SearchOutcome.NO_RESULTS
        max_pages = search.load_search_config("flipkart").browse_max_pages
        assert len(fetched) <= max_pages


class TestWhatCountsAsAHit:
    def test_only_exact_matches_are_returned(
        self, index: None, fetched: list[str]
    ) -> None:
        """A browse page has no relevance ranking, so a partial match is noise.

        The same reasoning the catalogue route uses: the page is ordered by the shop, and
        every other phone on it matches the query no better than by accident.
        """
        hits = BrowseSearch("flipkart", "phone").search("Samsung Galaxy S25", CTX).hits

        assert hits, "the exact match was dropped"
        assert all(hit.score >= 1.0 for hit in hits)
        assert not any("M06" in hit.title for hit in hits)

    def test_a_different_model_is_flagged_rather_than_hidden(
        self, index: None, fetched: list[str]
    ) -> None:
        """The FE is on the same page and matches every query word. It comes back
        qualified, so a person can see it is a different phone."""
        hits = BrowseSearch("flipkart", "phone").search("Samsung Galaxy S25", CTX).hits

        fe = [hit for hit in hits if "FE" in hit.title]
        assert fe and fe[0].qualifiers == ("fe",)
        assert not fe[0].is_exact

    def test_prices_come_back_with_the_hits(
        self, index: None, fetched: list[str]
    ) -> None:
        """The whole advantage of a browse page over a sitemap."""
        hits = BrowseSearch("flipkart", "phone").search("Samsung Galaxy S25", CTX).hits

        assert hits[0].price is not None
        assert not hits[0].from_sitemap


class TestRobots:
    def test_a_disallowed_listing_is_not_fetched(
        self, monkeypatch: pytest.MonkeyPatch, fetched: list[str]
    ) -> None:
        monkeypatch.setattr(sitemaps, "product_urls", lambda *a, **k: BROWSE_URLS)
        monkeypatch.setattr(robots, "is_allowed", lambda url, ctx: False)

        result = BrowseSearch("flipkart", "phone").search("Samsung Galaxy S25", CTX)

        assert result.outcome is SearchOutcome.DISALLOWED
        assert fetched == []
