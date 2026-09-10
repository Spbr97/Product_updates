"""API authentication, body-size limits, and the enriched readiness probe."""

from __future__ import annotations

from collections.abc import Iterator

import httpx
import pytest
import respx
from fastapi.testclient import TestClient
from tests.unit.test_adapters import load

from product_tracker.api.app import create_app
from product_tracker.api.deps import API_KEY_HEADER

pytestmark = pytest.mark.db

KEY = "s3cret-api-key"
URL = "https://shop.example.com/p/auth"


@pytest.fixture(autouse=True)
def _respx_router() -> Iterator[None]:
    with respx.mock:
        yield


@pytest.fixture
def open_client(clean_db: None) -> Iterator[TestClient]:
    """No API_KEY configured: the API is open, as on a localhost install."""
    with TestClient(create_app(), raise_server_exceptions=False) as client:
        yield client


@pytest.fixture
def secured_client(
    clean_db: None, monkeypatch: pytest.MonkeyPatch
) -> Iterator[TestClient]:
    from product_tracker.core.config import reset_settings_cache

    monkeypatch.setenv("API_KEY", KEY)
    reset_settings_cache()
    with TestClient(create_app(), raise_server_exceptions=False) as client:
        yield client


@pytest.fixture
def locked_client(clean_db: None, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """Reads locked down too."""
    from product_tracker.core.config import reset_settings_cache

    monkeypatch.setenv("API_KEY", KEY)
    monkeypatch.setenv("API_ALLOW_ANONYMOUS_READS", "false")
    reset_settings_cache()
    with TestClient(create_app(), raise_server_exceptions=False) as client:
        yield client


class TestAuthDisabled:
    def test_writes_are_open(self, open_client: TestClient) -> None:
        assert open_client.post("/api/v1/products", json={"url": URL}).status_code == 201

    def test_reads_are_open(self, open_client: TestClient) -> None:
        assert open_client.get("/api/v1/products").status_code == 200


class TestWritesRequireAKey:
    def test_create_without_a_key_is_401(self, secured_client: TestClient) -> None:
        response = secured_client.post("/api/v1/products", json={"url": URL})

        assert response.status_code == 401
        assert response.json()["error"]["type"] == "unauthorized"

    def test_the_challenge_names_the_header(self, secured_client: TestClient) -> None:
        """A client needs to be told how to authenticate."""
        response = secured_client.post("/api/v1/products", json={"url": URL})

        assert API_KEY_HEADER in response.headers.get("www-authenticate", "")

    def test_wrong_key_is_401(self, secured_client: TestClient) -> None:
        response = secured_client.post(
            "/api/v1/products", json={"url": URL}, headers={API_KEY_HEADER: "nope"}
        )
        assert response.status_code == 401

    def test_correct_key_is_accepted(self, secured_client: TestClient) -> None:
        response = secured_client.post(
            "/api/v1/products", json={"url": URL}, headers={API_KEY_HEADER: KEY}
        )
        assert response.status_code == 201

    def test_reads_stay_open_by_default(self, secured_client: TestClient) -> None:
        assert secured_client.get("/api/v1/products").status_code == 200

    @pytest.mark.parametrize(
        ("method", "path"),
        [
            ("delete", "/api/v1/products/1"),
            ("post", "/api/v1/products/1/check"),
            ("post", "/api/v1/products/1/pause"),
            ("post", "/api/v1/products/1/resume"),
            ("post", "/api/v1/alerts"),
            ("delete", "/api/v1/alerts/1"),
        ],
    )
    def test_every_mutating_route_is_guarded(
        self, secured_client: TestClient, method: str, path: str
    ) -> None:
        """401 must come before the 404 -- an unauthenticated caller learns nothing."""
        # TestClient.delete() takes no json body.
        response = (
            secured_client.delete(path)
            if method == "delete"
            else secured_client.post(path, json={})
        )

        assert response.status_code == 401


class TestLockedDownReads:
    def test_reads_require_a_key(self, locked_client: TestClient) -> None:
        assert locked_client.get("/api/v1/products").status_code == 401

    def test_reads_work_with_a_key(self, locked_client: TestClient) -> None:
        response = locked_client.get(
            "/api/v1/products", headers={API_KEY_HEADER: KEY}
        )
        assert response.status_code == 200

    def test_stores_listing_is_also_guarded(self, locked_client: TestClient) -> None:
        assert locked_client.get("/api/v1/stores").status_code == 401


class TestProbesStayOpen:
    """A probe should not need a credential."""

    def test_health_needs_no_key(self, locked_client: TestClient) -> None:
        assert locked_client.get("/health").status_code == 200

    def test_readiness_needs_no_key(self, locked_client: TestClient) -> None:
        assert locked_client.get("/health/ready").status_code == 200

    def test_openapi_needs_no_key(self, locked_client: TestClient) -> None:
        assert locked_client.get("/openapi.json").status_code == 200


class TestTheWorkerDoesNotGateTheApi:
    """A dead worker must not take the API down with it.

    The failure-isolation list the project is built against says it in as many words:
    *worker failure != API failure*. An API that can serve reads and accept new products
    is doing its job whether or not anything is checking prices, and the health route's
    own docstring says so -- but nothing asserted it, and "make readiness accurate" is a
    change somebody makes in good faith. Under Kubernetes it would pull every API pod out
    of the load balancer because a single worker died.
    """

    def stop_the_worker(self, seconds_ago: int = 86_400) -> None:
        """One heartbeat, aged past any staleness limit. That reads as *not running*,
        which is a stronger case than a fresh install where none has ever reported."""
        from sqlalchemy import text

        from product_tracker.db.session import session_scope
        from product_tracker.scheduler import heartbeat

        with session_scope() as session:
            heartbeat.touch(session, "worker-that-died")
            session.execute(
                text(
                    "UPDATE worker_heartbeats "
                    "SET last_seen_at = now() - make_interval(secs => :s)"
                ),
                {"s": seconds_ago},
            )

    def test_readiness_is_still_ready(self, open_client: TestClient) -> None:
        self.stop_the_worker()

        response = open_client.get("/health/ready")

        assert response.status_code == 200
        assert response.json()["status"] == "ready"

    def test_but_the_worker_is_reported_as_unhealthy(self, open_client: TestClient) -> None:
        """Isolation is not silence. The probe must still say the worker is gone --
        otherwise the API looks fine and nobody learns prices stopped updating."""
        self.stop_the_worker()

        dependencies = open_client.get("/health/ready").json()["dependencies"]
        scheduler = next(d for d in dependencies if d["name"] == "scheduler")

        assert scheduler["healthy"] is False
        assert "over the" in scheduler["detail"]

    def test_reads_and_writes_still_work(self, open_client: TestClient) -> None:
        """The part that actually matters to a user."""
        self.stop_the_worker()
        respx.get(URL).mock(return_value=httpx.Response(200, html=load("jsonld_in_stock.html")))

        created = open_client.post("/api/v1/products", json={"url": URL})
        listed = open_client.get("/api/v1/products")

        assert created.status_code == 201
        assert listed.status_code == 200


class TestBodySizeLimit:
    def test_an_oversized_body_is_rejected(self, open_client: TestClient) -> None:
        response = open_client.post(
            "/api/v1/products", json={"url": URL, "params": "x" * 200_000}
        )

        assert response.status_code == 413
        assert response.json()["error"]["type"] == "payload_too_large"

    def test_a_normal_body_passes(self, open_client: TestClient) -> None:
        assert open_client.post("/api/v1/products", json={"url": URL}).status_code == 201


class TestReadiness:
    def test_reports_every_dependency(self, open_client: TestClient) -> None:
        body = open_client.get("/health/ready").json()

        names = {dep["name"] for dep in body["dependencies"]}
        assert names == {"database", "scheduler", "notifications", "auth"}

    def test_ready_when_the_database_is_up(self, open_client: TestClient) -> None:
        response = open_client.get("/health/ready")

        assert response.status_code == 200
        assert response.json()["status"] == "ready"

    def test_no_worker_does_not_make_the_api_unready(
        self, open_client: TestClient
    ) -> None:
        """An API serving reads and accepting products is doing its job."""
        body = open_client.get("/health/ready").json()

        assert body["status"] == "ready"
        scheduler = next(d for d in body["dependencies"] if d["name"] == "scheduler")
        assert "job" in scheduler["detail"] or "scheduled" in scheduler["detail"]

    def test_auth_status_is_reported(self, secured_client: TestClient) -> None:
        body = secured_client.get("/health/ready").json()

        auth = next(d for d in body["dependencies"] if d["name"] == "auth")
        assert "API key required" in auth["detail"]

    def test_open_api_is_flagged(self, open_client: TestClient) -> None:
        body = open_client.get("/health/ready").json()

        auth = next(d for d in body["dependencies"] if d["name"] == "auth")
        assert "disabled" in auth["detail"]

    def test_no_secret_appears_anywhere(self, secured_client: TestClient) -> None:
        assert KEY not in secured_client.get("/health/ready").text


class TestAlertPagination:
    def test_filtered_listing_paginates_in_sql(self, open_client: TestClient) -> None:
        respx.get(URL).mock(return_value=httpx.Response(200, html=load("jsonld_in_stock.html")))
        product_id = open_client.post("/api/v1/products", json={"url": URL}).json()["id"]
        for rule_type in ("price_dropped", "price_increased", "price_changed"):
            open_client.post(
                "/api/v1/alerts", json={"product_id": product_id, "rule_type": rule_type}
            )

        body = open_client.get(
            "/api/v1/alerts", params={"product_id": product_id, "limit": 2}
        ).json()

        assert body["total"] == 3
        assert len(body["items"]) == 2


class TestRateLimit:
    @pytest.fixture
    def limited_client(
        self, clean_db: None, monkeypatch: pytest.MonkeyPatch
    ) -> Iterator[TestClient]:
        from product_tracker.core.config import reset_settings_cache

        monkeypatch.setenv("API_RATE_LIMIT_PER_MINUTE", "60")
        monkeypatch.setenv("API_RATE_LIMIT_BURST", "3")
        reset_settings_cache()
        with TestClient(create_app(), raise_server_exceptions=False) as client:
            yield client

    def test_a_burst_is_allowed_then_limited(self, limited_client: TestClient) -> None:
        codes = [
            limited_client.post(
                "/api/v1/products", json={"url": f"https://shop.example.com/p/rl{i}"}
            ).status_code
            for i in range(5)
        ]

        assert codes[:3] == [201, 201, 201]
        assert 429 in codes

    def test_the_limit_response_uses_the_error_envelope(
        self, limited_client: TestClient
    ) -> None:
        for i in range(6):
            response = limited_client.post(
                "/api/v1/products", json={"url": f"https://shop.example.com/p/env{i}"}
            )
            if response.status_code == 429:
                assert response.json()["error"]["type"] == "rate_limited"
                assert int(response.headers["retry-after"]) >= 1
                return
        pytest.fail("never hit the limit")

    def test_reads_are_not_limited(self, limited_client: TestClient) -> None:
        for _ in range(10):
            limited_client.post("/api/v1/products", json={"url": "https://shop.example.com/x"})

        assert limited_client.get("/api/v1/products").status_code == 200

    def test_probes_are_not_limited(self, limited_client: TestClient) -> None:
        for _ in range(10):
            limited_client.post("/api/v1/products", json={"url": "https://shop.example.com/y"})

        assert limited_client.get("/health").status_code == 200
        assert limited_client.get("/health/ready").status_code == 200
