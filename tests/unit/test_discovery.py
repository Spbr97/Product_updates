"""Fanning a query out across stores, and what is done with the answers."""

from __future__ import annotations

from decimal import Decimal

from product_tracker.domain.enums import SearchOutcome
from product_tracker.domain.models import SearchHit, SearchResult
from product_tracker.services.discovery import (
    Discovery,
    searchable_stores,
    unsearchable_stores,
)


def hit(store: str, title: str, score: float, qualifiers: tuple[str, ...] = ()) -> SearchHit:
    return SearchHit(
        url=f"https://{store}.example/p/{title.replace(' ', '-')}",
        title=title,
        store_slug=store,
        price=Decimal("79999"),
        score=score,
        qualifiers=qualifiers,
    )


def ok(store: str, *hits: SearchHit) -> SearchResult:
    return SearchResult(store_slug=store, outcome=SearchOutcome.OK, hits=hits)


class TestRanking:
    def test_hits_are_ordered_best_first_across_stores(self) -> None:
        found = Discovery(
            query="Galaxy S25",
            results=(
                ok("amazon-in", hit("amazon-in", "Galaxy S25 FE", 1.0, ("fe",))),
                ok("flipkart", hit("flipkart", "Galaxy S25", 1.0)),
            ),
        )
        assert found.hits[0].store_slug == "flipkart"

    def test_exact_excludes_qualified_matches(self) -> None:
        found = Discovery(
            query="Galaxy S25",
            results=(
                ok(
                    "amazon-in",
                    hit("amazon-in", "Galaxy S25", 1.0),
                    hit("amazon-in", "Galaxy S25 FE", 1.0, ("fe",)),
                ),
            ),
        )
        assert [h.title for h in found.exact] == ["Galaxy S25"]


class TestBestPerStore:
    def test_one_candidate_per_store(self) -> None:
        found = Discovery(
            query="Galaxy S25",
            results=(
                ok(
                    "amazon-in",
                    hit("amazon-in", "Galaxy S25 A", 1.0),
                    hit("amazon-in", "Galaxy S25 B", 1.0),
                ),
                ok("flipkart", hit("flipkart", "Galaxy S25 C", 1.0)),
            ),
        )
        best = found.best_per_store()
        assert set(best) == {"amazon-in", "flipkart"}
        assert best["amazon-in"].title == "Galaxy S25 A"

    def test_near_matches_are_excluded_by_default(self) -> None:
        """The default has to be strict.

        A "Galaxy S25 FE" scores a perfect 1.0 against a search for "Galaxy S25" -- every
        word is there. Including it by default means silently tracking a different phone.
        """
        found = Discovery(
            query="Galaxy S25",
            results=(ok("amazon-in", hit("amazon-in", "Galaxy S25 FE", 1.0, ("fe",))),),
        )
        assert found.best_per_store() == {}
        assert set(found.best_per_store(exact_only=False)) == {"amazon-in"}


class TestFailuresAreVisible:
    def test_a_blocked_store_is_reported_not_dropped(self) -> None:
        """A shop that refused us has said nothing about whether it stocks the product."""
        found = Discovery(
            query="Galaxy S25",
            results=(
                ok("flipkart", hit("flipkart", "Galaxy S25", 1.0)),
                SearchResult.failure("croma", SearchOutcome.BLOCKED, "403"),
            ),
        )
        assert [r.store_slug for r in found.unsearchable] == ["croma"]
        # The working store's answer is unaffected.
        assert len(found.hits) == 1

    def test_no_results_is_distinct_from_blocked(self) -> None:
        found = Discovery(
            query="x",
            results=(
                SearchResult(store_slug="a", outcome=SearchOutcome.NO_RESULTS),
                SearchResult(store_slug="b", outcome=SearchOutcome.BLOCKED),
            ),
        )
        outcomes = {r.store_slug: r.outcome for r in found.unsearchable}
        assert outcomes["a"] is SearchOutcome.NO_RESULTS
        assert outcomes["b"] is SearchOutcome.BLOCKED


class TestCoverage:
    def test_searchable_and_unsearchable_partition_the_catalogue(self) -> None:
        overlap = set(searchable_stores()) & set(unsearchable_stores())
        assert overlap == set()

    def test_the_gaps_are_nameable(self) -> None:
        """Stores with no search yet are listed, so the gap is visible rather than silent."""
        assert "croma" in unsearchable_stores()

    def test_at_least_one_store_is_searchable(self) -> None:
        assert searchable_stores()
