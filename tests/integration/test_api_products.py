"""Product and store API endpoints."""

from __future__ import annotations

from collections.abc import Iterator

import httpx
import pytest
import respx
from fastapi.testclient import TestClient
from tests.unit.test_adapters import load

from product_tracker.api.app import create_app

pytestmark = pytest.mark.db

PRODUCT_URL = "https://shop.example.com/p/api-1"


@pytest.fixture(autouse=True)
def _respx_router() -> Iterator[None]:
    """Activate respx for every test in this module.

    Not ``@respx.mock`` on the class: in respx 0.23 that decorator returns a *function*,
    so pytest silently stops collecting the class and the tests never run.
    """
    with respx.mock:
        yield


@pytest.fixture
def client(clean_db: None) -> Iterator[TestClient]:
    with TestClient(create_app(), raise_server_exceptions=False) as test_client:
        yield test_client


def stub_ok(url: str) -> None:
    respx.get(url).mock(return_value=httpx.Response(200, html=load("jsonld_in_stock.html")))


class TestCreate:
    def test_creates_a_product(self, client: TestClient) -> None:
        response = client.post("/api/v1/products", json={"url": PRODUCT_URL})

        assert response.status_code == 201
        body = response.json()
        assert body["url"] == PRODUCT_URL
        assert body["store"]["slug"] == "generic"
        assert body["availability"] == "unknown"
        assert body["tracking_status"] == "active"

    def test_duplicate_returns_409(self, client: TestClient) -> None:
        client.post("/api/v1/products", json={"url": PRODUCT_URL})

        response = client.post("/api/v1/products", json={"url": PRODUCT_URL})

        assert response.status_code == 409
        assert response.json()["error"]["type"] == "conflict"

    @pytest.mark.parametrize(
        "url", ["ftp://example.com/x", "not-a-url", "https://user:pw@example.com/x"]
    )
    def test_invalid_url_returns_422(self, client: TestClient, url: str) -> None:
        response = client.post("/api/v1/products", json={"url": url})

        assert response.status_code == 422
        assert response.json()["error"]["type"] == "validation_error"

    def test_ssrf_attempt_returns_422(
        self, client: TestClient, strict_url_policy: None
    ) -> None:
        """An IP literal needs no DNS, so the guard can be exercised hermetically."""
        response = client.post(
            "/api/v1/products", json={"url": "http://169.254.169.254/latest/meta-data/"}
        )

        assert response.status_code == 422
        assert "non-public" in response.json()["error"]["message"]

    def test_missing_url_field_returns_422(self, client: TestClient) -> None:
        assert client.post("/api/v1/products", json={}).status_code == 422

    def test_interval_below_minimum_is_rejected(self, client: TestClient) -> None:
        response = client.post(
            "/api/v1/products", json={"url": PRODUCT_URL, "check_interval_seconds": 5}
        )
        assert response.status_code == 422


class TestListAndGet:
    def test_lists_with_pagination_envelope(self, client: TestClient) -> None:
        for index in range(3):
            client.post("/api/v1/products", json={"url": f"https://shop.example.com/p/{index}"})

        response = client.get("/api/v1/products", params={"limit": 2})

        assert response.status_code == 200
        body = response.json()
        assert len(body["items"]) == 2
        assert body["total"] == 3
        assert body["limit"] == 2
        assert body["offset"] == 0

    def test_limit_is_clamped_to_the_configured_maximum(self, client: TestClient) -> None:
        """An oversized limit is clamped, not rejected -- the caller still gets a page."""
        response = client.get("/api/v1/products", params={"limit": 100_000})

        assert response.status_code == 200
        assert response.json()["limit"] == 100  # api_max_page_size

    def test_limit_below_one_is_rejected(self, client: TestClient) -> None:
        assert client.get("/api/v1/products", params={"limit": 0}).status_code == 422

    def test_filters_by_store(self, client: TestClient) -> None:
        client.post("/api/v1/products", json={"url": "https://www.flipkart.com/x/p/itm1"})
        client.post("/api/v1/products", json={"url": PRODUCT_URL})

        assert client.get("/api/v1/products", params={"store": "flipkart"}).json()["total"] == 1

    def test_get_one(self, client: TestClient) -> None:
        created = client.post("/api/v1/products", json={"url": PRODUCT_URL}).json()

        response = client.get(f"/api/v1/products/{created['id']}")

        assert response.status_code == 200
        assert response.json()["id"] == created["id"]

    def test_get_missing_returns_404(self, client: TestClient) -> None:
        response = client.get("/api/v1/products/999999")

        assert response.status_code == 404
        assert response.json()["error"]["type"] == "not_found"


class TestDelete:
    def test_deletes_and_then_404s(self, client: TestClient) -> None:
        created = client.post("/api/v1/products", json={"url": PRODUCT_URL}).json()

        assert client.delete(f"/api/v1/products/{created['id']}").status_code == 204
        assert client.get(f"/api/v1/products/{created['id']}").status_code == 404

    def test_delete_missing_returns_404(self, client: TestClient) -> None:
        assert client.delete("/api/v1/products/999999").status_code == 404


class TestCheck:
    def test_check_returns_the_execution(self, client: TestClient) -> None:
        stub_ok(PRODUCT_URL)
        created = client.post("/api/v1/products", json={"url": PRODUCT_URL}).json()

        response = client.post(f"/api/v1/products/{created['id']}/check")

        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "success"
        assert body["extracted_price"] == "69999.00"
        assert body["availability_result"] == "in_stock"

    def test_store_failure_still_returns_200_with_a_failed_execution(
        self, client: TestClient
    ) -> None:
        """A store we cannot read is a recorded fact, not a broken API."""
        url = "https://shop.example.com/p/blocked-api"
        respx.get(url).mock(return_value=httpx.Response(403))
        created = client.post("/api/v1/products", json={"url": url}).json()

        response = client.post(f"/api/v1/products/{created['id']}/check")

        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "failed"
        assert body["error_type"] == "blocked"
        assert body["availability_result"] == "unknown"

    def test_check_missing_product_returns_404(self, client: TestClient) -> None:
        assert client.post("/api/v1/products/999999/check").status_code == 404


class TestStores:
    def test_lists_supported_stores(self, client: TestClient) -> None:
        response = client.get("/api/v1/stores")

        assert response.status_code == 200
        by_slug = {store["slug"]: store for store in response.json()}
        assert set(by_slug) == {"flipkart", "generic"}
        assert by_slug["generic"]["is_fallback"] is True
        assert by_slug["flipkart"]["is_fallback"] is False
        assert "flipkart.com" in by_slug["flipkart"]["domains"]


class TestOpenApi:
    def test_new_paths_are_documented(self, client: TestClient) -> None:
        paths = client.get("/openapi.json").json()["paths"]

        assert "/api/v1/products" in paths
        assert "/api/v1/products/{product_id}" in paths
        assert "/api/v1/products/{product_id}/check" in paths
        assert "/api/v1/stores" in paths
