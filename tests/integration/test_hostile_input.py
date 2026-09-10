"""Input the database refuses, and what a crash writes into the log.

All three cases here came out of driving the live API as four different accounts with
deliberately hostile input. Each returned a 500, and a 500 for bad input is two problems:
it tells the caller the server broke when the caller is the one at fault, and it sends
whoever is on call hunting a fault that is not there.

The third is the one that mattered. An unhandled exception used to render every stack
frame's *locals*, and one of those frames is the ASGI middleware whose ``scope`` holds the
raw request headers -- so a crash wrote the caller's API key into the log in full. The
redaction processor could not catch it: it scans the event's own keys, and the traceback
arrives underneath them as an already-rendered blob.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from product_tracker.api.app import create_app
from product_tracker.services import user_service

pytestmark = pytest.mark.db

#: Larger than a PostgreSQL bigint, so the driver refuses it before any row is read.
TOO_BIG = 99999999999999999999
NUL = "x%00y"


@pytest.fixture
def account(db_session) -> str:  # type: ignore[no-untyped-def]
    created = user_service.create_user(db_session, email="hostile@example.com")
    db_session.commit()
    return str(created.api_key)


@pytest.fixture
def client(clean_db: None, account: str) -> Iterator[tuple[TestClient, dict[str, str]]]:
    with TestClient(create_app(), raise_server_exceptions=False) as test_client:
        yield test_client, {"X-API-Key": account}


class TestValuesTheDatabaseRefuses:
    def test_an_id_too_large_for_the_column_is_not_a_crash(
        self, client: tuple[TestClient, dict[str, str]]
    ) -> None:
        test_client, headers = client

        response = test_client.get(f"/api/v1/products/{TOO_BIG}", headers=headers)

        assert response.status_code == 422
        assert response.json()["error"]["type"] == "validation_error"

    def test_a_nul_byte_in_a_slug_is_not_a_crash(
        self, client: tuple[TestClient, dict[str, str]]
    ) -> None:
        """PostgreSQL text cannot hold 0x00, and psycopg says so rather than truncating."""
        test_client, headers = client

        response = test_client.get(f"/api/v1/groups/{NUL}", headers=headers)

        assert response.status_code in {404, 422}
        assert response.status_code != 500

    def test_the_driver_s_message_is_not_passed_on(
        self, client: tuple[TestClient, dict[str, str]]
    ) -> None:
        """It carries the SQL and the bound parameters, which is nobody's business."""
        test_client, headers = client

        body = test_client.get(f"/api/v1/products/{TOO_BIG}", headers=headers).text.lower()

        for leak in ("select", "psycopg", "sqlalchemy", "postgresql", "traceback"):
            assert leak not in body, f"the response carried {leak!r}"


class TestACrashDoesNotLogTheCredential:
    """The reason tracebacks are rendered without frame locals."""

    def test_the_api_key_never_reaches_the_log(
        self, client: tuple[TestClient, dict[str, str]], caplog: pytest.LogCaptureFixture
    ) -> None:
        test_client, headers = client
        key = headers["X-API-Key"]

        with caplog.at_level(logging.DEBUG):
            test_client.get(f"/api/v1/products/{TOO_BIG}", headers=headers)
            test_client.get(f"/api/v1/groups/{NUL}", headers=headers)

        written = "\n".join(record.getMessage() for record in caplog.records)
        assert key not in written, "the caller's API key was written to the log"

    def test_a_rendered_traceback_carries_no_locals(self) -> None:
        """Directly, rather than through a request: the renderer is the thing under test,
        and a future change to it would otherwise only surface as a silent leak."""
        import structlog

        from product_tracker.core.logging import configure_logging

        configure_logging(fmt="json")
        capture = structlog.testing.LogCapture()
        processors = structlog.get_config()["processors"]
        # Keep every processor except the final renderer, so the exception is transformed
        # exactly as configured and then captured instead of printed.
        structlog.configure(processors=[*processors[:-1], capture])

        secret = "pt_thisisthesecretvalue"
        try:
            _local_holding_a_secret = secret
            raise ValueError("boom")
        except ValueError as exc:
            structlog.get_logger(__name__).error("test.crash", exc_info=exc)
        finally:
            configure_logging()

        rendered = json.dumps(capture.entries, default=str)
        assert secret not in rendered, "a frame local reached the rendered traceback"
