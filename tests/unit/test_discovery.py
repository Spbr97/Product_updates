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


class FakeSearch:
    """Records how it was called and answers from a script."""

    def __init__(self, slug: str, http: SearchResult, rendered: SearchResult | None) -> None:
        self.slug = slug
        self._http = http
        self._rendered = rendered
        self.calls: list[bool] = []

    def search(
        self, query: str, ctx: object, *, limit: int = 10, use_browser: bool = False
    ) -> SearchResult:
        self.calls.append(use_browser)
        if use_browser and self._rendered is not None:
            return self._rendered
        return self._http

    @property
    def rendered_once(self) -> bool:
        return True in self.calls


def unreadable(store: str) -> SearchResult:
    return SearchResult.failure(store, SearchOutcome.PAGE_STRUCTURE, "no product links")


class EscalationHarness:
    """Wires fake stores and a fake browser into ``discover``."""

    def __init__(  # type: ignore[no-untyped-def]
        self,
        monkeypatch,
        searches: dict[str, FakeSearch],
        render_modes: dict[str, str],
        sitemaps: dict[str, str] | None = None,
    ):
        import contextlib

        from product_tracker.services import discovery as module
        from product_tracker.stores.search import SearchConfig

        self.searches = searches
        self.sessions = 0
        sitemaps = sitemaps or {}

        @contextlib.contextmanager
        def fake_session(**_kwargs: object):  # type: ignore[no-untyped-def]
            self.sessions += 1
            yield None

        def fake_config(slug: str) -> SearchConfig:
            return SearchConfig(
                url_template=f"https://{slug}.example/s?q={{query}}",
                result_link=(),
                product_url_pattern="/p/",
                render=render_modes.get(slug, "http"),
                # These tests are about the render escalation. Catalogues are off unless a
                # test asks for them, so a sitemap pass cannot quietly answer instead.
                sitemap=sitemaps.get(slug, "none"),
            )

        monkeypatch.setattr(module, "search_for", lambda slug: searches.get(slug))
        monkeypatch.setattr(module, "load_search_config", fake_config)
        monkeypatch.setattr(module.browser, "session", fake_session)
        monkeypatch.setattr(module, "searchable_stores", lambda: tuple(searches))


class TestEscalation:
    """When the expensive pass is and is not worth running."""

    @staticmethod
    def settings():  # type: ignore[no-untyped-def]
        from product_tracker.core.config import Settings

        return Settings(database_url="postgresql+psycopg://x/y")

    @staticmethod
    def instant_guard():  # type: ignore[no-untyped-def]
        """A guard that paces nothing.

        Discovery now builds a real throttle by default, which is right in production and
        turns a millisecond test suite into a minute of real sleeping. StoreGuard takes an
        injectable sleeper precisely so tests can be deterministic rather than slow.
        """
        from product_tracker.scheduler.throttle import StoreGuard

        return StoreGuard(
            min_interval_seconds=0.0,
            jitter_seconds=0.0,
            failure_threshold=99,
            reset_seconds=0.0,
            sleeper=lambda _seconds: None,
        )

    def run(self, monkeypatch, searches, render_modes, **kwargs):  # type: ignore[no-untyped-def]
        from product_tracker.services.discovery import discover

        EscalationHarness(monkeypatch, searches, render_modes)
        kwargs.setdefault("guard", self.instant_guard())
        return discover("Galaxy S25", self.settings(), **kwargs)

    def test_an_exact_match_on_the_cheap_pass_skips_rendering(self, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        """The whole point of escalating rather than always rendering."""
        searches = {
            "fast": FakeSearch("fast", ok("fast", hit("fast", "Galaxy S25", 1.0)), None),
            "slow": FakeSearch("slow", unreadable("slow"), None),
        }
        found = self.run(monkeypatch, searches, {"slow": "auto"})

        assert found.exact
        assert not searches["slow"].rendered_once

    def test_no_exact_match_triggers_rendering(self, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        rendered = ok("slow", hit("slow", "Galaxy S25", 1.0))
        searches = {
            "fast": FakeSearch("fast", ok("fast", hit("fast", "iPhone", 0.2)), None),
            "slow": FakeSearch("slow", unreadable("slow"), rendered),
        }
        found = self.run(monkeypatch, searches, {"slow": "auto"})

        assert searches["slow"].rendered_once
        assert [h.store_slug for h in found.exact] == ["slow"]

    def test_near_matches_alone_still_trigger_rendering(self, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        """A Galaxy S25 FE is not an answer to "Galaxy S25".

        Triggering on "no hits at all" would stop here and call the question answered.
        """
        searches = {
            "fast": FakeSearch(
                "fast", ok("fast", hit("fast", "Galaxy S25 FE", 1.0, ("fe",))), None
            ),
            "slow": FakeSearch("slow", unreadable("slow"), ok("slow", hit("slow", "S25", 1.0))),
        }
        self.run(monkeypatch, searches, {"slow": "auto"})

        assert searches["slow"].rendered_once

    def test_no_browser_never_renders(self, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        searches = {"slow": FakeSearch("slow", unreadable("slow"), None)}
        self.run(monkeypatch, searches, {"slow": "auto"}, allow_browser=False)

        assert not searches["slow"].rendered_once

    def test_an_http_only_store_is_never_rendered(self, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        searches = {"plain": FakeSearch("plain", unreadable("plain"), None)}
        self.run(monkeypatch, searches, {"plain": "http"})

        assert not searches["plain"].rendered_once

    def test_a_blocked_store_is_not_re_asked_through_a_browser(self, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        """Re-asking a refusal through a browser is the first step to working around it."""
        blocked = SearchResult.failure("shy", SearchOutcome.BLOCKED, "403")
        searches = {"shy": FakeSearch("shy", blocked, ok("shy", hit("shy", "S25", 1.0)))}
        self.run(monkeypatch, searches, {"shy": "auto"})

        assert not searches["shy"].rendered_once

    def test_one_browser_serves_the_whole_render_pass(self, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        from product_tracker.services.discovery import discover

        searches = {
            name: FakeSearch(name, unreadable(name), unreadable(name))
            for name in ("a", "b", "c")
        }
        harness = EscalationHarness(monkeypatch, searches, dict.fromkeys(searches, "auto"))
        discover("Galaxy S25", self.settings(), guard=self.instant_guard())

        assert harness.sessions == 1

    def test_a_missing_browser_stops_the_pass_rather_than_repeating_the_failure(
        self, monkeypatch
    ) -> None:  # type: ignore[no-untyped-def]
        """Every remaining store would say the same thing; asking each is wasted work."""
        missing = SearchResult.failure("a", SearchOutcome.NEEDS_BROWSER, "install the extra")
        searches = {
            name: FakeSearch(name, unreadable(name), missing) for name in ("a", "b", "c")
        }
        found = self.run(monkeypatch, searches, dict.fromkeys(searches, "auto"))

        assert searches["a"].rendered_once
        assert not searches["c"].rendered_once
        # But every store still reports the reason, rather than a stale "unreadable".
        outcomes = {r.store_slug: r.outcome for r in found.unsearchable}
        assert set(outcomes.values()) == {SearchOutcome.NEEDS_BROWSER}


class TestPacing:
    """Search is not exempt from politeness because a person typed it."""

    def test_a_guard_is_built_when_none_is_given(self, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        """The gap that let this tool get a retailer to stop answering it.

        ``discover`` took a guard, the CLI never passed one, and the HTTP pass was not
        guarded at all -- so a fan-out hit every shop as fast as the network allowed, and
        repeating that hard enough got one of them to block us.
        """
        from product_tracker.core.config import Settings
        from product_tracker.services import discovery as module

        built: list[object] = []
        original = module._default_guard
        monkeypatch.setattr(
            module,
            "_default_guard",
            lambda settings: built.append(original(settings)) or built[-1],
        )
        searches = {"a": FakeSearch("a", ok("a", hit("a", "Galaxy S25", 1.0)), None)}
        EscalationHarness(monkeypatch, searches, {})

        module.discover("Galaxy S25", Settings(database_url="postgresql+psycopg://x/y"))

        assert built, "discover must pace itself even when the caller forgets to ask"

    def test_every_pass_goes_through_the_guard(self, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        """Including the cheap HTTP one, which was previously unthrottled."""
        from product_tracker.core.config import Settings
        from product_tracker.services import discovery as module

        seen: list[str] = []

        class RecordingGuard:
            def before(self, host: str):  # type: ignore[no-untyped-def]
                from product_tracker.domain.models import GuardDecision

                seen.append(host)
                return GuardDecision.go()

            def after(self, host: str, *, succeeded: bool) -> None: ...

        searches = {
            "a": FakeSearch("a", ok("a", hit("a", "Galaxy S25", 1.0)), None),
            "b": FakeSearch("b", ok("b", hit("b", "Galaxy S25", 1.0)), None),
        }
        EscalationHarness(monkeypatch, searches, {})
        module.discover(
            "Galaxy S25",
            Settings(database_url="postgresql+psycopg://x/y"),
            guard=RecordingGuard(),
        )

        assert len(seen) == 2

    def test_a_throttled_store_is_reported_not_silently_skipped(self, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        from product_tracker.core.config import Settings
        from product_tracker.domain.models import GuardDecision
        from product_tracker.services import discovery as module

        class RefusingGuard:
            def before(self, host: str) -> GuardDecision:
                return GuardDecision.skip("circuit open")

            def after(self, host: str, *, succeeded: bool) -> None: ...

        searches = {"a": FakeSearch("a", ok("a", hit("a", "Galaxy S25", 1.0)), None)}
        EscalationHarness(monkeypatch, searches, {})
        found = module.discover(
            "Galaxy S25",
            Settings(database_url="postgresql+psycopg://x/y"),
            guard=RefusingGuard(),
        )

        assert not searches["a"].calls
        assert [r.store_slug for r in found.unsearchable] == ["a"]
