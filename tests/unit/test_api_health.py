"""Liveness endpoint and the error envelope.

``/health`` must answer without touching the database -- these tests deliberately point
settings at a DSN that nothing is listening on, and still expect 200.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from product_tracker import __version__
from product_tracker.api.app import create_app


@pytest.fixture
def client(dummy_env: None) -> TestClient:
    return TestClient(create_app(), raise_server_exceptions=False)


class TestLiveness:
    def test_health_is_ok_without_a_database(self, client: TestClient) -> None:
        response = client.get("/health")

        assert response.status_code == 200
        assert response.json() == {
            "status": "ok",
            "version": __version__,
            "service": "product-tracker",
        }

    def test_readiness_reports_not_ready_when_database_is_down(
        self, client: TestClient
    ) -> None:
        """A dead dependency makes the process unready, but never unhealthy."""
        response = client.get("/health/ready")

        assert response.status_code == 503
        body = response.json()
        assert body["status"] == "not_ready"
        database = next(dep for dep in body["dependencies"] if dep["name"] == "database")
        assert database["healthy"] is False

    def test_readiness_detail_does_not_leak_the_dsn(self, client: TestClient) -> None:
        """Connection errors can carry the password; only the exception type is exposed."""
        body = client.get("/health/ready").json()
        assert "pass" not in str(body)
        assert "unit_tests" not in str(body)


class TestOpenAPI:
    def test_schema_is_served(self, client: TestClient) -> None:
        schema = client.get("/openapi.json").json()

        assert schema["info"]["title"] == "Product Tracker"
        assert schema["info"]["version"] == __version__
        assert "/health" in schema["paths"]

    def test_docs_are_served(self, client: TestClient) -> None:
        assert client.get("/docs").status_code == 200


class TestErrorEnvelope:
    def test_unknown_route_uses_the_error_envelope(self, client: TestClient) -> None:
        response = client.get("/api/v1/nope")

        assert response.status_code == 404
        assert response.json() == {
            "error": {"type": "not_found", "message": "Not Found"}
        }

    def test_method_not_allowed_uses_the_error_envelope(self, client: TestClient) -> None:
        response = client.post("/health")

        assert response.status_code == 405
        assert response.json()["error"]["type"] == "method_not_allowed"
