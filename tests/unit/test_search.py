"""Store search: ranking, parsing, and refusing to guess.

The behaviour worth guarding hardest is that a near-match is reported as a near-match. A
search for "Galaxy S25" returns the S25 FE too -- a different phone, four hundred pounds
cheaper -- and anything that quietly treats the two as interchangeable will eventually have
somebody tracking the wrong product and celebrating a bargain that does not exist.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from tests.unit.test_adapters import load

from product_tracker.domain.enums import SearchOutcome
from product_tracker.domain.models import SearchHit, SearchResult
from product_tracker.stores.http import FetchSuccess
from product_tracker.stores.search import (
    ConfiguredSearch,
    available_searches,
    load_search_config,
    score_title,
    search_for,
    tokenise,
)


class TestScoring:
    def test_an_exact_title_scores_full(self) -> None:
        score, qualifiers = score_title("Galaxy S25", "Samsung Galaxy S25 5G (Navy, 256 GB)")
        assert score == 1.0
        assert qualifiers == ()

    def test_a_missing_word_lowers_the_score(self) -> None:
        score, _ = score_title("iPhone 17", "Apple iPhone 16 (Black, 256 GB)")
        assert score == 0.5

    def test_an_extra_model_word_is_reported(self) -> None:
        """The S25 FE case, which is the entire reason qualifiers exist."""
        score, qualifiers = score_title("Galaxy S25", "Samsung Galaxy S25 FE 8GB 256GB Navy")

        # Every word asked for is present, so the score alone calls it a perfect match.
        assert score == 1.0
        # But "FE" is a different phone, and that is said out loud.
        assert qualifiers == ("fe",)

    @pytest.mark.parametrize(
        ("title", "expected"),
        [
            ("Apple iPhone 17 Pro Max 256GB", ("max", "pro")),
            ("Samsung Galaxy S25 Ultra", ("ultra",)),
            ("Apple iPhone 17 (Black, 256 GB)", ()),
            ("Apple iPhone 17 Renewed", ("renewed",)),
        ],
    )
    def test_qualifier_detection(self, title: str, expected: tuple[str, ...]) -> None:
        _score, qualifiers = score_title("iPhone 17", title)
        assert qualifiers == expected

    def test_a_word_the_query_asked_for_is_not_a_qualifier(self) -> None:
        """Searching for a Pro should not flag every Pro result as a different model."""
        score, qualifiers = score_title("iPhone 17 Pro", "Apple iPhone 17 Pro 256GB")
        assert score == 1.0
        assert qualifiers == ()

    def test_an_empty_query_scores_nothing(self) -> None:
        assert score_title("", "Anything") == (0.0, ())

    def test_tokenise_splits_on_punctuation(self) -> None:
        assert tokenise("iPhone 17 (Black, 256 GB)") == ["iphone", "17", "black", "256", "gb"]


class TestSearchHit:
    def test_exact_requires_full_score_and_no_qualifier(self) -> None:
        assert SearchHit(url="u", title="t", store_slug="s", score=1.0).is_exact

    def test_a_qualified_hit_is_not_exact(self) -> None:
        hit = SearchHit(url="u", title="t", store_slug="s", score=1.0, qualifiers=("fe",))
        assert not hit.is_exact

    def test_a_partial_match_is_not_exact(self) -> None:
        assert not SearchHit(url="u", title="t", store_slug="s", score=0.9).is_exact


class TestParsing:
    """Against a saved Amazon results page, structured as the real one is."""

    @staticmethod
    def parse(query: str, limit: int = 10) -> tuple[SearchHit, ...]:
        search = ConfiguredSearch("amazon-in")
        response = FetchSuccess(
            html=load("amazon_search.html"),
            url="https://www.amazon.in/s?k=galaxy+s25",
            http_status=200,
        )
        return search._parse(response, load_search_config("amazon-in"), query, limit=limit)

    def test_finds_the_products(self) -> None:
        hits = self.parse("Galaxy S25")
        assert len(hits) == 3

    def test_reads_titles_and_prices(self) -> None:
        best = self.parse("Galaxy S25")[0]
        assert "Galaxy S25" in best.title
        assert best.price == Decimal("79999")

    def test_the_brand_is_restored_to_the_title(self) -> None:
        """Amazon keeps the brand in its own element.

        Without this a Xiaomi 17 is titled "17 (12GB/512GB)" and reads exactly like an
        iPhone 17 -- in a tool whose whole job is knowing which product a listing is.
        """
        titles = [hit.title for hit in self.parse("17")]
        assert any(title.startswith("XIAOMI") for title in titles)

    def test_a_different_model_is_flagged_not_hidden(self) -> None:
        hits = {hit.title: hit for hit in self.parse("Galaxy S25")}
        fe = next(hit for title, hit in hits.items() if "FE" in title)

        assert fe.qualifiers == ("fe",)
        assert not fe.is_exact

    def test_exact_matches_rank_above_qualified_ones(self) -> None:
        hits = self.parse("Galaxy S25")
        assert hits[0].is_exact
        assert not hits[-1].is_exact

    def test_the_price_taken_is_the_one_charged_not_the_mrp(self) -> None:
        """Amazon shows the price then the struck-out list price; order matters."""
        best = self.parse("Galaxy S25")[0]
        assert best.price == Decimal("79999")
        assert best.price != Decimal("89999")

    def test_click_trackers_are_not_treated_as_products(self) -> None:
        for hit in self.parse("Galaxy S25"):
            assert "/gp/slredirect/" not in hit.url

    def test_the_same_product_is_not_returned_twice(self) -> None:
        """A card links the product from its image, title and rating."""
        urls = [hit.url.split("?")[0] for hit in self.parse("Galaxy S25")]
        assert len(urls) == len(set(urls))

    def test_limit_is_respected(self) -> None:
        assert len(self.parse("Galaxy S25", limit=1)) == 1


class TestOutcomes:
    def test_an_unreadable_page_is_not_reported_as_no_results(self) -> None:
        """"We could not read the page" and "they do not stock it" are different facts.

        Reporting the first as the second tells someone a shop has no such product when
        the shop may simply have rendered its results with JavaScript.
        """
        search = ConfiguredSearch("amazon-in")
        response = FetchSuccess(
            html="<html><body>no results markup here</body></html>",
            url="https://www.amazon.in/s?k=x",
            http_status=200,
        )
        parsed = search._parse(response, load_search_config("amazon-in"), "x", limit=5)
        assert parsed == ()

    def test_failure_result_carries_its_reason(self) -> None:
        result = SearchResult.failure("croma", SearchOutcome.BLOCKED, "403", http_status=403)
        assert not result.succeeded
        assert result.outcome is SearchOutcome.BLOCKED
        assert result.http_status == 403


class TestConfiguration:
    def test_every_shipped_config_loads(self) -> None:
        """A malformed YAML should fail here, not silently find nothing in production."""
        for slug in available_searches():
            config = load_search_config(slug)
            assert config.url_template
            assert "{query}" in config.url_template
            assert config.product_url_pattern

    def test_a_store_without_a_search_returns_none(self) -> None:
        assert search_for("croma") is None

    def test_a_configured_store_returns_a_search(self) -> None:
        assert search_for("amazon-in") is not None


class TestFlipkartParsing:
    """Flipkart's results, whose block is the product link itself."""

    @staticmethod
    def parse(query: str = "Galaxy S25", limit: int = 10) -> tuple[SearchHit, ...]:
        search = ConfiguredSearch("flipkart")
        response = FetchSuccess(
            html=load("flipkart_search.html"),
            url="https://www.flipkart.com/search?q=Galaxy+S25",
            http_status=200,
        )
        return search._parse(response, load_search_config("flipkart"), query, limit=limit)

    def test_a_card_that_is_its_own_link_is_found(self) -> None:
        """Regression: ``select`` only looks at descendants.

        Flipkart's config anchors the result block on the href shape, because its class
        names rotate. That makes the card and the link the same element, and looking only
        inside it found no link at all -- every result silently skipped, the store blamed
        for returning nothing.
        """
        assert len(self.parse()) == 3

    def test_reads_titles_and_selling_prices(self) -> None:
        best = self.parse()[0]
        assert best.title == "Samsung Galaxy S25 5G (Silver Shadow, 256 GB)"
        assert best.price == Decimal("79999")

    def test_an_exchange_offer_is_never_taken_as_the_price(self) -> None:
        """The nastiest trap on the page.

        "Upto Rs 54,400 Off on Exchange" is *lower* than the Rs 79,999 price, so picking it
        up does not look like a bug -- it looks like a deal, and it would be recorded in
        price history as one.
        """
        for hit in self.parse():
            assert hit.price != Decimal("54400")

    def test_navigation_links_are_not_products(self) -> None:
        for hit in self.parse():
            assert "/p/itm" in hit.url

    def test_a_different_model_is_flagged(self) -> None:
        hits = {hit.title: hit for hit in self.parse()}
        fe = next(hit for title, hit in hits.items() if " FE " in title)
        assert fe.qualifiers == ("fe",)
        assert not fe.is_exact


class TestConfigValidation:
    def test_an_unknown_key_is_refused(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        """Silently ignoring a key is how two stores kept a renamed one and broke.

        They carried "result_container" after it became "result_card", parsed the whole
        page as a single result, and could never return more than one hit -- while looking
        like the stores were at fault.
        """
        import product_tracker.stores.search as search_module
        from product_tracker.domain.errors import ConfigurationError

        (tmp_path / "bogus.yaml").write_text(
            'url: "https://x.example/s?q={query}"\n'
            'product_url_pattern: "/p/"\n'
            'result_container: "div"\n',
            encoding="utf-8",
        )
        original = search_module.SEARCH_DIR
        search_module.SEARCH_DIR = tmp_path
        try:
            load_search_config.cache_clear()
            with pytest.raises(ConfigurationError, match="result_container"):
                load_search_config("bogus")
        finally:
            search_module.SEARCH_DIR = original
            load_search_config.cache_clear()
