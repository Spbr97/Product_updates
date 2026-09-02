"""The search endpoint: finding a product by name over HTTP.

Discovery's own ranking and fan-out are covered in ``tests/unit/test_discovery.py``. What
matters here is the HTTP contract: that a caller is told which shops could not answer and
why, that an unhonourable request is refused rather than quietly narrowed, and that
searching tracks nothing.

``discover`` is substituted throughout. The suite must not depend on a retailer being
online, and a fan-out across four shops is not something to stub selector by selector.
"""

from __future__ import annotations

from collections.abc import Iterator
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from product_tracker.api.app import create_app
from product_tracker.api.deps import API_KEY_HEADER
from product_tracker.core.config import Settings
from product_tracker.domain.enums import SearchOutcome
from product_tracker.domain.models import SearchHit, SearchResult
from product_tracker.services import discovery
from product_tracker.services.discovery import Discovery

pytestmark = pytest.mark.db

SEARCH = "/api/v1/search"


@pytest.fixture
def client(clean_db: None) -> Iterator[TestClient]:
    with TestClient(create_app(), raise_server_exceptions=False) as test_client:
        yield test_client


def hit(
    store: str,
    title: str,
    *,
    score: float = 1.0,
    qualifiers: tuple[str, ...] = (),
    price: str | None = "79999",
    from_sitemap: bool = False,
) -> SearchHit:
    return SearchHit(
        url=f"https://{store}.example/p/{title.replace(' ', '-').lower()}",
        title=title,
        store_slug=store,
        price=None if price is None else Decimal(price),
        currency=None if price is None else "INR",
        score=score,
        qualifiers=qualifiers,
        from_sitemap=from_sitemap,
    )


def ok(store: str, *hits: SearchHit) -> SearchResult:
    return SearchResult(store_slug=store, outcome=SearchOutcome.OK, hits=hits)


def stub(monkeypatch: pytest.MonkeyPatch, *results: SearchResult) -> list[dict[str, object]]:
    """Replace the fan-out, and record how each call was made."""
    calls: list[dict[str, object]] = []

    def fake(
        query: str,
        settings: Settings,
        *,
        store_slugs: tuple[str, ...] | None = None,
        limit_per_store: int = 8,
        allow_browser: bool = True,
        guard: object = None,
    ) -> Discovery:
        calls.append(
            {
                "query": query,
                "store_slugs": store_slugs,
                "limit_per_store": limit_per_store,
                "allow_browser": allow_browser,
            }
        )
        return Discovery(query=query, results=results)

    monkeypatch.setattr(discovery, "discover", fake)
    return calls


class TestResults:
    def test_returns_hits_best_first_with_store_names(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        stub(
            monkeypatch,
            ok("amazon-in", hit("amazon-in", "Galaxy S25 FE", qualifiers=("fe",))),
            ok("flipkart", hit("flipkart", "Galaxy S25")),
        )

        response = client.post(SEARCH, json={"query": "Galaxy S25"})

        assert response.status_code == 200, response.text
        body = response.json()
        assert [h["title"] for h in body["hits"]] == ["Galaxy S25", "Galaxy S25 FE"]
        # The display name, not the slug: a UI should not have to own that mapping.
        assert body["hits"][0]["store"] == "Flipkart"
        assert body["hits"][0]["store_slug"] == "flipkart"

    def test_qualifiers_and_exactness_are_exposed(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The one-word difference between two phones tens of thousands apart."""
        stub(
            monkeypatch,
            ok(
                "flipkart",
                hit("flipkart", "Galaxy S25"),
                hit("flipkart", "Galaxy S25 FE", qualifiers=("fe",)),
            ),
        )

        body = client.post(SEARCH, json={"query": "Galaxy S25"}).json()

        exact, near = body["hits"]
        assert exact["is_exact"] is True and exact["qualifiers"] == []
        assert near["is_exact"] is False and near["qualifiers"] == ["fe"]
        assert body["exact_count"] == 1

    def test_a_catalogue_hit_is_flagged_and_has_no_price(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Its title came off the URL rather than from the shop, so it is marked.

        Presenting a derived name as the retailer's own wording would be a small lie.
        """
        stub(
            monkeypatch,
            ok("vijay-sales", hit("vijay-sales", "Galaxy S25", price=None, from_sitemap=True)),
        )

        found = client.post(SEARCH, json={"query": "Galaxy S25"}).json()["hits"][0]

        assert found["from_sitemap"] is True
        assert found["price"] is None

    def test_searching_tracks_nothing(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        stub(monkeypatch, ok("flipkart", hit("flipkart", "Galaxy S25")))

        client.post(SEARCH, json={"query": "Galaxy S25"})

        assert client.get("/api/v1/products").json()["total"] == 0


class TestGapsAreReported:
    """A blocked shop must never read as a shop that had nothing."""

    def test_a_refusal_is_reported_with_its_reason(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        stub(
            monkeypatch,
            ok("flipkart", hit("flipkart", "Galaxy S25")),
            SearchResult.failure("amazon-in", SearchOutcome.BLOCKED, "refused", http_status=503),
        )

        body = client.post(SEARCH, json={"query": "Galaxy S25"}).json()

        assert len(body["hits"]) == 1
        (gap,) = body["gaps"]
        assert gap["store_slug"] == "amazon-in"
        assert gap["outcome"] == "blocked"
        assert gap["http_status"] == 503

    def test_no_results_is_distinct_from_blocked(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        stub(monkeypatch, SearchResult.failure("flipkart", SearchOutcome.NO_RESULTS, "none"))

        body = client.post(SEARCH, json={"query": "nonexistent thing"}).json()

        assert body["gaps"][0]["outcome"] == "no_results"
        assert body["hits"] == []

    def test_stores_with_no_search_configured_are_named(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Reported rather than omitted, so the gap in coverage is visible."""
        stub(monkeypatch, ok("flipkart", hit("flipkart", "Galaxy S25")))

        body = client.post(SEARCH, json={"query": "Galaxy S25"}).json()

        assert set(body["skipped"]) == set(discovery.unsearchable_stores())
        assert "flipkart" not in body["skipped"]


class TestStoreSelection:
    def test_searches_every_searchable_store_by_default(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls = stub(monkeypatch)

        body = client.post(SEARCH, json={"query": "Galaxy S25"}).json()

        assert calls[0]["store_slugs"] == discovery.searchable_stores()
        assert body["searched"] == list(discovery.searchable_stores())

    def test_restricting_to_one_store_searches_only_it(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls = stub(monkeypatch)

        client.post(SEARCH, json={"query": "Galaxy S25", "stores": ["flipkart"]})

        assert calls[0]["store_slugs"] == ("flipkart",)

    def test_an_unknown_store_is_refused_not_ignored(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Dropping it silently would answer "search Croma" with 200 and no results,
        which reads as "Croma has nothing"."""
        calls = stub(monkeypatch)

        response = client.post(SEARCH, json={"query": "Galaxy S25", "stores": ["croma"]})

        assert response.status_code == 422
        assert "croma" in response.json()["error"]["message"]
        assert calls == []

    def test_an_empty_store_list_is_refused(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls = stub(monkeypatch)

        response = client.post(SEARCH, json={"query": "Galaxy S25", "stores": []})

        assert response.status_code == 422
        assert calls == []

    def test_repeated_slugs_do_not_repeat_the_search(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls = stub(monkeypatch)

        client.post(SEARCH, json={"query": "x", "stores": ["flipkart"] * 10})

        assert calls[0]["store_slugs"] == ("flipkart",)


class TestRequestShape:
    def test_browser_rendering_is_off_unless_asked_for(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A request that can exceed a minute should not be the default one."""
        calls = stub(monkeypatch)

        client.post(SEARCH, json={"query": "Galaxy S25"})

        assert calls[0]["allow_browser"] is False

    def test_browser_rendering_can_be_requested(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls = stub(monkeypatch)

        client.post(SEARCH, json={"query": "Galaxy S25", "allow_browser": True})

        assert calls[0]["allow_browser"] is True

    def test_an_empty_query_is_refused(self, client: TestClient) -> None:
        assert client.post(SEARCH, json={"query": ""}).status_code == 422

    def test_an_absurdly_long_query_is_refused(self, client: TestClient) -> None:
        assert client.post(SEARCH, json={"query": "x" * 5000}).status_code == 422

    def test_the_per_store_limit_is_bounded(self, client: TestClient) -> None:
        response = client.post(SEARCH, json={"query": "x", "limit_per_store": 5000})
        assert response.status_code == 422


class TestItIsNotAGet:
    """The endpoint is a POST on purpose, and that must not quietly regress.

    ``ratelimit.LIMITED_METHODS`` exempts GET because "reads are cheap and local". A
    search is neither: it fans out to every configured retailer. As a GET this would be
    the one unmetered route in the API that causes outbound traffic to real shops.
    """

    def test_get_is_not_offered(self, client: TestClient) -> None:
        assert client.get(SEARCH).status_code == 405

    def test_the_method_is_one_the_rate_limiter_meters(self, client: TestClient) -> None:
        from product_tracker.api.ratelimit import LIMITED_METHODS

        # Read from the published schema rather than the route objects: how FastAPI
        # stores routes internally has changed between versions, what it publishes has not.
        paths = client.get("/openapi.json").json()["paths"]
        assert SEARCH in paths, "the search route is not mounted"
        methods = {method.upper() for method in paths[SEARCH]}
        assert methods <= LIMITED_METHODS


class TestItNeedsAWriteKey:
    """Search is guarded like a write, though it changes nothing here.

    What it spends is outbound: a fan-out to every configured retailer, and this
    deployment's standing with each of them. An anonymous caller should not be able to
    spend either. ``API_ALLOW_ANONYMOUS_READS`` deliberately does not open this.
    """

    KEY = "s3cret-api-key"

    @pytest.fixture
    def secured_client(
        self, clean_db: None, monkeypatch: pytest.MonkeyPatch
    ) -> Iterator[TestClient]:
        from product_tracker.core.config import reset_settings_cache

        monkeypatch.setenv("API_KEY", self.KEY)
        reset_settings_cache()
        with TestClient(create_app(), raise_server_exceptions=False) as test_client:
            yield test_client

    def test_without_a_key_it_is_401_and_nothing_is_fetched(
        self, secured_client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls = stub(monkeypatch)

        response = secured_client.post(SEARCH, json={"query": "Galaxy S25"})

        assert response.status_code == 401
        assert API_KEY_HEADER in response.headers.get("www-authenticate", "")
        # The point of the guard: no shop was contacted on an unauthenticated request.
        assert calls == []

    def test_a_wrong_key_is_refused(
        self, secured_client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls = stub(monkeypatch)

        response = secured_client.post(
            SEARCH, json={"query": "Galaxy S25"}, headers={API_KEY_HEADER: "nope"}
        )

        assert response.status_code == 401
        assert calls == []

    def test_the_right_key_searches(
        self, secured_client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        stub(monkeypatch, ok("flipkart", hit("flipkart", "Galaxy S25")))

        response = secured_client.post(
            SEARCH, json={"query": "Galaxy S25"}, headers={API_KEY_HEADER: self.KEY}
        )

        assert response.status_code == 200
        assert len(response.json()["hits"]) == 1
