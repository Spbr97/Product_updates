"""The id that ties one request's log lines together.

Structured logging was already in place, and every field the contract names was there
except this one -- which is the one that makes the others useful. A log holding thousands
of interleaved requests can tell you a check failed; without a correlation id it cannot
tell you which of them belonged to the person who complained.

The plumbing existed the whole time: ``configure_logging`` runs ``merge_contextvars``
first, so anything bound for the request appears on every line it produces. Only the
binding was missing.
"""

from __future__ import annotations

from collections.abc import Iterator

import httpx
import pytest
import respx
import structlog
from fastapi.testclient import TestClient
from tests.unit.test_adapters import load

from product_tracker.api.app import create_app
from product_tracker.api.middleware import REQUEST_ID_HEADER

SHOP_URL = "https://shop.example.com/p/request-id"

pytestmark = pytest.mark.db


@pytest.fixture(autouse=True)
def _respx_router() -> Iterator[None]:
    with respx.mock:
        yield


@pytest.fixture
def client(clean_db: None) -> Iterator[TestClient]:
    with TestClient(create_app(), raise_server_exceptions=False) as test_client:
        yield test_client


@pytest.fixture
def captured() -> Iterator[list[dict[str, object]]]:
    """Capture what the loggers actually emitted, rather than trusting the header."""
    capture = structlog.testing.LogCapture()
    # merge_contextvars ahead of the capture, exactly as configure_logging orders it.
    # Without it the binding never reaches the entry and this would test nothing.
    structlog.configure(processors=[structlog.contextvars.merge_contextvars, capture])
    yield capture.entries
    from product_tracker.core.logging import configure_logging

    configure_logging()


class TestEveryResponseCarriesOne:
    def test_a_successful_request_gets_an_id(self, client: TestClient) -> None:
        response = client.get("/health")

        assert response.headers.get(REQUEST_ID_HEADER)

    def test_two_requests_get_different_ids(self, client: TestClient) -> None:
        first = client.get("/health").headers[REQUEST_ID_HEADER]
        second = client.get("/health").headers[REQUEST_ID_HEADER]

        assert first != second

    def test_a_failing_request_gets_one_too(self, client: TestClient) -> None:
        """The one someone actually asks about. An id that only appears on success is
        worth very little."""
        response = client.get("/api/v1/products/99999999")

        assert response.status_code == 404
        assert response.headers.get(REQUEST_ID_HEADER)

    def test_a_rejected_body_still_gets_one(self, client: TestClient) -> None:
        """Rejected by middleware, before routing. This is why the id binds outermost."""
        response = client.post(
            "/api/v1/products", json={"url": "https://shop.example.com/p/1", "pad": "x" * 200_000}
        )

        assert response.status_code == 413
        assert response.headers.get(REQUEST_ID_HEADER)


class TestAClientMaySupplyIt:
    def test_a_supplied_id_is_kept(self, client: TestClient) -> None:
        """So an id set by a proxy, or by a caller correlating across services, survives
        into our logs rather than being replaced by one only we know."""
        response = client.get("/health", headers={REQUEST_ID_HEADER: "trace-abc-123"})

        assert response.headers[REQUEST_ID_HEADER] == "trace-abc-123"

    def test_a_hostile_id_cannot_shape_the_log_line(self, client: TestClient) -> None:
        """It is untrusted input that ends up in a log. Newlines would let a caller forge
        entries; the length cap stops one request filling the log on its own."""
        response = client.get(
            "/health", headers={REQUEST_ID_HEADER: "a\nb\rc evil " + "x" * 500}
        )

        echoed = response.headers[REQUEST_ID_HEADER]
        assert "\n" not in echoed and "\r" not in echoed and " " not in echoed
        assert len(echoed) <= 64

    def test_an_empty_id_is_replaced_rather_than_honoured(self, client: TestClient) -> None:
        response = client.get("/health", headers={REQUEST_ID_HEADER: ""})

        assert response.headers.get(REQUEST_ID_HEADER)


class TestItReachesTheLogs:
    """The point of the exercise. Not the header -- the log.

    These run a real check rather than hitting ``/health``, because ``/health`` writes no
    log lines at all and a test asserting "every line carries the id" against zero lines
    asserts nothing. A check is the path that actually produces the chain the correlation
    id exists to join up: check.started, fetch.result, check.finished.
    """

    @pytest.fixture
    def product_id(self, client: TestClient) -> int:
        respx.get(SHOP_URL).mock(
            return_value=httpx.Response(200, html=load("jsonld_in_stock.html"))
        )
        return int(client.post("/api/v1/products", json={"url": SHOP_URL}).json()["id"])

    def test_every_line_the_request_writes_carries_it(
        self, client: TestClient, product_id: int, captured: list[dict[str, object]]
    ) -> None:
        respx.get(SHOP_URL).mock(
            return_value=httpx.Response(200, html=load("jsonld_in_stock.html"))
        )

        client.post(
            f"/api/v1/products/{product_id}/check",
            headers={REQUEST_ID_HEADER: "trace-xyz"},
        )

        assert captured, "the request produced no log lines at all"
        assert all(entry.get("request_id") == "trace-xyz" for entry in captured)

    def test_it_reaches_the_engine_not_just_the_router(
        self, client: TestClient, product_id: int, captured: list[dict[str, object]]
    ) -> None:
        """Bound once at the edge and picked up several layers down, with nothing in
        between passing it along. That is what contextvars buy, and what a plain argument
        would have cost every call site."""
        respx.get(SHOP_URL).mock(
            return_value=httpx.Response(200, html=load("jsonld_in_stock.html"))
        )

        client.post(f"/api/v1/products/{product_id}/check", headers={REQUEST_ID_HEADER: "deep"})

        events = {entry.get("event") for entry in captured}
        assert "check.started" in events, f"expected an engine log line, saw {events}"

    def test_it_does_not_leak_into_the_next_request(
        self, client: TestClient, product_id: int, captured: list[dict[str, object]]
    ) -> None:
        """Workers are reused. A binding left in place would stamp the next request with
        the previous one's id, which is worse than having none: it invents a connection
        between two unrelated requests."""
        respx.get(SHOP_URL).mock(
            return_value=httpx.Response(200, html=load("jsonld_in_stock.html"))
        )
        client.post(f"/api/v1/products/{product_id}/check", headers={REQUEST_ID_HEADER: "first"})
        captured.clear()
        client.post(f"/api/v1/products/{product_id}/check", headers={REQUEST_ID_HEADER: "second"})

        ids = {entry.get("request_id") for entry in captured}
        assert "first" not in ids
