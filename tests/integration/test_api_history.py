"""History, availability, and statistics endpoints."""

from __future__ import annotations

from collections.abc import Iterator

import httpx
import pytest
import respx
from fastapi.testclient import TestClient
from tests.unit.test_adapters import load

from product_tracker.api.app import create_app

pytestmark = pytest.mark.db

URL = "https://shop.example.com/p/api-history"


@pytest.fixture
def client(clean_db: None) -> Iterator[TestClient]:
    with TestClient(create_app(), raise_server_exceptions=False) as test_client:
        yield test_client


def create_and_check(client: TestClient, url: str = URL, html: str | None = None) -> int:
    """Track a product and run one check against a stubbed page."""
    respx.get(url).mock(
        return_value=httpx.Response(200, html=html or load("jsonld_in_stock.html"))
    )
    product_id = client.post("/api/v1/products", json={"url": url}).json()["id"]
    client.post(f"/api/v1/products/{product_id}/check")
    return int(product_id)


@respx.mock
class TestPriceHistory:
    def test_returns_recorded_observations(self, client: TestClient) -> None:
        product_id = create_and_check(client)

        response = client.get(f"/api/v1/products/{product_id}/history")

        assert response.status_code == 200
        body = response.json()
        assert body["total"] == 1
        assert body["items"][0]["price"] == "69999.00"
        assert body["items"][0]["currency"] == "INR"

    def test_entries_cite_the_check_that_produced_them(self, client: TestClient) -> None:
        product_id = create_and_check(client)

        entry = client.get(f"/api/v1/products/{product_id}/history").json()["items"][0]

        assert entry["check_execution_id"] is not None

    def test_empty_history_is_an_empty_page_not_a_404(self, client: TestClient) -> None:
        product_id = client.post("/api/v1/products", json={"url": URL}).json()["id"]

        response = client.get(f"/api/v1/products/{product_id}/history")

        assert response.status_code == 200
        assert response.json() == {"items": [], "total": 0, "limit": 20, "offset": 0}

    def test_missing_product_returns_404(self, client: TestClient) -> None:
        response = client.get("/api/v1/products/999999/history")

        assert response.status_code == 404
        assert response.json()["error"]["type"] == "not_found"

    def test_pagination_envelope(self, client: TestClient) -> None:
        product_id = create_and_check(client)

        body = client.get(
            f"/api/v1/products/{product_id}/history", params={"limit": 1}
        ).json()

        assert body["limit"] == 1
        assert body["offset"] == 0


@respx.mock
class TestAvailabilityHistory:
    def test_returns_transitions(self, client: TestClient) -> None:
        product_id = create_and_check(client)

        response = client.get(f"/api/v1/products/{product_id}/availability")

        assert response.status_code == 200
        body = response.json()
        assert body["total"] == 1
        assert body["items"][0]["availability"] == "in_stock"

    def test_missing_product_returns_404(self, client: TestClient) -> None:
        assert client.get("/api/v1/products/999999/availability").status_code == 404


@respx.mock
class TestStats:
    def test_reports_aggregates(self, client: TestClient) -> None:
        product_id = create_and_check(client)

        response = client.get(f"/api/v1/products/{product_id}/stats")

        assert response.status_code == 200
        body = response.json()
        assert body["observations"] == 1
        assert body["current"] == "69999.00"
        assert body["lowest"] == "69999.00"
        assert body["highest"] == "69999.00"
        assert body["currency"] == "INR"
        assert body["mixed_currency"] is False

    def test_null_when_nothing_recorded_yet(self, client: TestClient) -> None:
        """A tracked but unchecked product is not an error."""
        product_id = client.post("/api/v1/products", json={"url": URL}).json()["id"]

        response = client.get(f"/api/v1/products/{product_id}/stats")

        assert response.status_code == 200
        assert response.json() is None

    def test_missing_product_returns_404(self, client: TestClient) -> None:
        assert client.get("/api/v1/products/999999/stats").status_code == 404

    def test_tracks_a_price_drop(self, client: TestClient) -> None:
        product_id = create_and_check(client)
        respx.get(URL).mock(
            return_value=httpx.Response(
                200, html=load("jsonld_in_stock.html").replace("69999.00", "59999.00")
            )
        )
        client.post(f"/api/v1/products/{product_id}/check")

        body = client.get(f"/api/v1/products/{product_id}/stats").json()

        assert body["observations"] == 2
        assert body["current"] == "59999.00"
        assert body["lowest"] == "59999.00"
        assert body["highest"] == "69999.00"
        assert body["changed_by"] == "-10000.00"
        assert body["lowest_at"] is not None


class TestOpenApi:
    def test_history_paths_are_documented(self, client: TestClient) -> None:
        paths = client.get("/openapi.json").json()["paths"]

        assert "/api/v1/products/{product_id}/history" in paths
        assert "/api/v1/products/{product_id}/availability" in paths
        assert "/api/v1/products/{product_id}/stats" in paths
