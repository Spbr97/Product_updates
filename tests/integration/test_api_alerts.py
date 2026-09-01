"""Alert endpoints and pause/resume."""

from __future__ import annotations

from collections.abc import Iterator

import httpx
import pytest
import respx
from fastapi.testclient import TestClient
from tests.unit.test_adapters import load

from product_tracker.api.app import create_app

pytestmark = pytest.mark.db

URL = "https://shop.example.com/p/api-alerts"


@pytest.fixture(autouse=True)
def _respx_router() -> Iterator[None]:
    with respx.mock:
        yield


@pytest.fixture
def client(clean_db: None) -> Iterator[TestClient]:
    with TestClient(create_app(), raise_server_exceptions=False) as test_client:
        yield test_client


@pytest.fixture
def product_id(client: TestClient) -> int:
    respx.get(URL).mock(return_value=httpx.Response(200, html=load("jsonld_in_stock.html")))
    return int(client.post("/api/v1/products", json={"url": URL}).json()["id"])


class TestCreate:
    def test_creates_a_rule(self, client: TestClient, product_id: int) -> None:
        response = client.post(
            "/api/v1/alerts", json={"product_id": product_id, "rule_type": "price_dropped"}
        )

        assert response.status_code == 201
        body = response.json()
        assert body["rule_type"] == "price_dropped"
        assert body["enabled"] is True
        assert body["last_fired_at"] is None

    def test_target_price_is_stored_as_a_param(
        self, client: TestClient, product_id: int
    ) -> None:
        response = client.post(
            "/api/v1/alerts",
            json={
                "product_id": product_id,
                "rule_type": "price_below_target",
                "target_price": "69999.00",
            },
        )

        assert response.status_code == 201
        assert response.json()["params"]["target_price"] == "69999.00"

    def test_target_price_is_required_for_that_rule(
        self, client: TestClient, product_id: int
    ) -> None:
        response = client.post(
            "/api/v1/alerts",
            json={"product_id": product_id, "rule_type": "price_below_target"},
        )

        assert response.status_code == 422
        assert "target_price" in response.json()["error"]["message"]

    def test_negative_target_is_rejected_by_the_schema(
        self, client: TestClient, product_id: int
    ) -> None:
        response = client.post(
            "/api/v1/alerts",
            json={
                "product_id": product_id,
                "rule_type": "price_below_target",
                "target_price": "-5",
            },
        )
        assert response.status_code == 422

    def test_unknown_rule_type_is_rejected(
        self, client: TestClient, product_id: int
    ) -> None:
        response = client.post(
            "/api/v1/alerts", json={"product_id": product_id, "rule_type": "price_halved"}
        )
        assert response.status_code == 422

    def test_duplicate_rule_type_returns_409(
        self, client: TestClient, product_id: int
    ) -> None:
        payload = {"product_id": product_id, "rule_type": "price_dropped"}
        client.post("/api/v1/alerts", json=payload)

        assert client.post("/api/v1/alerts", json=payload).status_code == 409

    def test_missing_product_returns_404(self, client: TestClient) -> None:
        response = client.post(
            "/api/v1/alerts", json={"product_id": 999999, "rule_type": "price_dropped"}
        )
        assert response.status_code == 404

    def test_unknown_provider_returns_422(
        self, client: TestClient, product_id: int
    ) -> None:
        response = client.post(
            "/api/v1/alerts",
            json={
                "product_id": product_id,
                "rule_type": "price_dropped",
                "notify_provider": "carrier-pigeon",
            },
        )
        assert response.status_code == 422


class TestListGetDelete:
    def test_lists_with_pagination(self, client: TestClient, product_id: int) -> None:
        client.post(
            "/api/v1/alerts", json={"product_id": product_id, "rule_type": "price_dropped"}
        )
        client.post(
            "/api/v1/alerts", json={"product_id": product_id, "rule_type": "price_increased"}
        )

        body = client.get("/api/v1/alerts").json()

        assert body["total"] == 2
        assert len(body["items"]) == 2

    def test_filters_by_product(self, client: TestClient, product_id: int) -> None:
        client.post(
            "/api/v1/alerts", json={"product_id": product_id, "rule_type": "price_dropped"}
        )

        body = client.get("/api/v1/alerts", params={"product_id": product_id}).json()

        assert body["total"] == 1

    def test_filter_by_missing_product_returns_404(self, client: TestClient) -> None:
        assert client.get("/api/v1/alerts", params={"product_id": 999999}).status_code == 404

    def test_get_one(self, client: TestClient, product_id: int) -> None:
        created = client.post(
            "/api/v1/alerts", json={"product_id": product_id, "rule_type": "price_dropped"}
        ).json()

        assert client.get(f"/api/v1/alerts/{created['id']}").status_code == 200

    def test_get_missing_returns_404(self, client: TestClient) -> None:
        assert client.get("/api/v1/alerts/999999").status_code == 404

    def test_delete(self, client: TestClient, product_id: int) -> None:
        created = client.post(
            "/api/v1/alerts", json={"product_id": product_id, "rule_type": "price_dropped"}
        ).json()

        assert client.delete(f"/api/v1/alerts/{created['id']}").status_code == 204
        assert client.get(f"/api/v1/alerts/{created['id']}").status_code == 404

    def test_delete_missing_returns_404(self, client: TestClient) -> None:
        assert client.delete("/api/v1/alerts/999999").status_code == 404


class TestPauseResume:
    def test_pause_then_resume(self, client: TestClient, product_id: int) -> None:
        paused = client.post(f"/api/v1/products/{product_id}/pause")
        assert paused.status_code == 200
        assert paused.json()["tracking_status"] == "paused"

        resumed = client.post(f"/api/v1/products/{product_id}/resume")
        assert resumed.json()["tracking_status"] == "active"

    def test_pausing_keeps_the_product_listed(
        self, client: TestClient, product_id: int
    ) -> None:
        client.post(f"/api/v1/products/{product_id}/pause")

        body = client.get("/api/v1/products", params={"tracking_status": "paused"}).json()

        assert body["total"] == 1

    def test_a_manual_check_still_works_while_paused(
        self, client: TestClient, product_id: int
    ) -> None:
        """Pausing stops the scheduler, not you."""
        client.post(f"/api/v1/products/{product_id}/pause")

        response = client.post(f"/api/v1/products/{product_id}/check")

        assert response.status_code == 200
        assert response.json()["status"] == "success"

    def test_pause_missing_product_returns_404(self, client: TestClient) -> None:
        assert client.post("/api/v1/products/999999/pause").status_code == 404


class TestOpenApi:
    def test_alert_paths_are_documented(self, client: TestClient) -> None:
        paths = client.get("/openapi.json").json()["paths"]

        assert "/api/v1/alerts" in paths
        assert "/api/v1/alerts/{rule_id}" in paths
        assert "/api/v1/products/{product_id}/pause" in paths
        assert "/api/v1/products/{product_id}/resume" in paths
