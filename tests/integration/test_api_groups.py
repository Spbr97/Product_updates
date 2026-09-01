"""Group and comparison endpoints."""

from __future__ import annotations

from collections.abc import Iterator

import httpx
import pytest
import respx
from fastapi.testclient import TestClient
from tests.unit.test_adapters import load

from product_tracker.api.app import create_app

pytestmark = pytest.mark.db


@pytest.fixture(autouse=True)
def _respx_router() -> Iterator[None]:
    """Activate respx for every test here; the class decorator form is not collected."""
    with respx.mock:
        yield


@pytest.fixture
def client(clean_db: None) -> Iterator[TestClient]:
    with TestClient(create_app(), raise_server_exceptions=False) as test_client:
        yield test_client


def track(client: TestClient, url: str, fixture: str = "jsonld_in_stock.html") -> int:
    """Track a listing whose page is stubbed, and return its id."""
    respx.get(url).mock(return_value=httpx.Response(200, html=load(fixture)))
    response = client.post("/api/v1/products", json={"url": url})
    assert response.status_code == 201, response.text
    return int(response.json()["id"])


class TestCreatingGroups:
    def test_creates_a_group(self, client: TestClient) -> None:
        response = client.post("/api/v1/groups", json={"name": "iPhone 17", "brand": "Apple"})

        assert response.status_code == 201
        body = response.json()
        assert body["slug"] == "iphone-17"
        assert body["brand"] == "Apple"

    def test_an_explicit_slug_is_honoured(self, client: TestClient) -> None:
        response = client.post("/api/v1/groups", json={"name": "iPhone 17", "slug": "ip17"})
        assert response.json()["slug"] == "ip17"

    def test_a_malformed_slug_is_rejected(self, client: TestClient) -> None:
        response = client.post("/api/v1/groups", json={"name": "x", "slug": "Not A Slug"})
        assert response.status_code == 422

    def test_a_duplicate_slug_conflicts(self, client: TestClient) -> None:
        client.post("/api/v1/groups", json={"name": "iPhone 17"})
        response = client.post("/api/v1/groups", json={"name": "iPhone 17"})
        assert response.status_code == 409

    def test_listing_and_fetching(self, client: TestClient) -> None:
        client.post("/api/v1/groups", json={"name": "iPhone 17"})

        assert [g["slug"] for g in client.get("/api/v1/groups").json()] == ["iphone-17"]
        assert client.get("/api/v1/groups/iphone-17").status_code == 200

    def test_unknown_group_is_a_404(self, client: TestClient) -> None:
        response = client.get("/api/v1/groups/nope")
        assert response.status_code == 404
        assert response.json()["error"]["type"] == "not_found"


class TestAttaching:
    def test_infers_the_model_from_the_url(self, client: TestClient) -> None:
        """``POST /products`` does not fetch the page, so a freshly tracked listing has no
        title yet. The URL slug is what inference has to work with at this point."""
        client.post("/api/v1/groups", json={"name": "iPhone 17"})
        product_id = track(client, "https://shop.example.com/apple-iphone-17-256gb-black/p/1")

        response = client.post(
            "/api/v1/groups/iphone-17/listings", json={"product_id": product_id}
        )

        assert response.status_code == 201
        assert response.json()["label"] == "256GB / Black"

    def test_an_unreadable_listing_is_refused_not_guessed(self, client: TestClient) -> None:
        client.post("/api/v1/groups", json={"name": "iPhone 17"})
        product_id = track(client, "https://shop.example.com/p/mystery-9")

        response = client.post(
            "/api/v1/groups/iphone-17/listings", json={"product_id": product_id}
        )

        assert response.status_code == 422

    def test_an_explicit_variant_is_honoured(self, client: TestClient) -> None:
        client.post("/api/v1/groups", json={"name": "iPhone 17"})
        product_id = track(client, "https://shop.example.com/p/attach-2")

        response = client.post(
            "/api/v1/groups/iphone-17/listings",
            json={"product_id": product_id, "variant": "512GB / Teal"},
        )

        assert response.json()["label"] == "512GB / Teal"

    def test_attaching_to_an_unknown_group_is_a_404(self, client: TestClient) -> None:
        product_id = track(client, "https://shop.example.com/p/attach-3")
        response = client.post(
            "/api/v1/groups/nope/listings", json={"product_id": product_id, "variant": "x"}
        )
        assert response.status_code == 404

    def test_detaching_keeps_the_listing(self, client: TestClient) -> None:
        client.post("/api/v1/groups", json={"name": "iPhone 17"})
        product_id = track(client, "https://shop.example.com/p/attach-4")
        client.post(
            "/api/v1/groups/iphone-17/listings",
            json={"product_id": product_id, "variant": "256GB / Black"},
        )

        response = client.delete(f"/api/v1/groups/iphone-17/listings/{product_id}")

        assert response.status_code == 204
        # Still tracked -- detaching removes the grouping, nothing else.
        assert client.get(f"/api/v1/products/{product_id}").status_code == 200


class TestComparing:
    def setup_group(self, client: TestClient) -> None:
        """Two models across two shops, with one square deliberately left empty.

        The gap is the point: the Sage is tracked at only one shop, so the other cell must
        come back as "not tracked" rather than as anything resembling a stock claim.
        """
        client.post("/api/v1/groups", json={"name": "iPhone 17", "brand": "Apple"})
        listings = [
            ("https://shop.example.com/p/cmp-1", "jsonld_in_stock.html", "256GB / Black"),
            ("https://www.flipkart.com/p/itmcmp2", "flipkart_product.html", "256GB / Black"),
            ("https://shop.example.com/p/cmp-3", "jsonld_in_stock.html", "256GB / Sage"),
        ]
        for url, fixture, variant in listings:
            product_id = track(client, url, fixture)
            response = client.post(
                "/api/v1/groups/iphone-17/listings",
                json={"product_id": product_id, "variant": variant},
            )
            assert response.status_code == 201, response.text

    def test_returns_models_by_shops(self, client: TestClient) -> None:
        self.setup_group(client)

        body = client.get("/api/v1/groups/iphone-17/compare").json()

        assert body["group_name"] == "iPhone 17"
        assert {row["label"] for row in body["rows"]} == {"256GB / Black", "256GB / Sage"}
        # Every shop column appears in every row, so a client can render a grid directly.
        columns = {store["slug"] for store in body["stores"]}
        for row in body["rows"]:
            assert set(row["cells"]) == columns

    def test_every_cell_carries_a_status(self, client: TestClient) -> None:
        """A client must never have to infer meaning from a null price."""
        self.setup_group(client)

        body = client.get("/api/v1/groups/iphone-17/compare").json()

        for row in body["rows"]:
            for cell in row["cells"].values():
                assert cell["status"]

    def test_untracked_shops_are_reported_as_such(self, client: TestClient) -> None:
        self.setup_group(client)

        body = client.get("/api/v1/groups/iphone-17/compare").json()
        statuses = {c["status"] for row in body["rows"] for c in row["cells"].values()}

        # Two models at one shop: the other model's cell is untracked, not out of stock.
        assert "not_tracked" in statuses
        assert "out_of_stock" not in statuses

    def test_comparing_an_unknown_group_is_a_404(self, client: TestClient) -> None:
        assert client.get("/api/v1/groups/nope/compare").status_code == 404

    def test_stale_hours_is_validated(self, client: TestClient) -> None:
        self.setup_group(client)
        assert client.get("/api/v1/groups/iphone-17/compare?stale_hours=0").status_code == 422
