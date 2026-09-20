"""The internal scheduler trigger: auth guard and response shape.

``run_all_checks`` itself -- claiming, fetching, recording -- is exercised elsewhere
(the CLI's ``check-all`` tests and the integration suite); these tests only cover what is
specific to the HTTP wrapper: the token guard, and that the summary it returns is what the
route reports back.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from product_tracker.api.app import create_app
from product_tracker.services.check_runner import CheckAllSummary, CheckOutcome
from product_tracker.services.notification_service import DeliveryReport

TOKEN = "s3cret-internal-token"


@pytest.fixture
def client(dummy_env: None, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("INTERNAL_SCHEDULER_TOKEN", TOKEN)
    from product_tracker.core.config import reset_settings_cache

    reset_settings_cache()
    return TestClient(create_app(), raise_server_exceptions=False)


def _outcome(product_id: int) -> CheckOutcome:
    from product_tracker.domain.enums import CheckStatus, FetchMethod

    return CheckOutcome(
        execution_id=product_id,
        product_id=product_id,
        status=CheckStatus.SUCCESS,
        fetch_method=FetchMethod.HTTP,
        availability=None,
        price=None,
        currency=None,
        duration_ms=1,
        attempts=1,
        error_type=None,
        error_detail=None,
        notifications_created=0,
    )


class TestTokenGuard:
    def test_missing_token_is_refused(self, client: TestClient) -> None:
        response = client.post("/internal/scheduler/check-all")

        assert response.status_code == 401
        assert response.json()["error"]["type"] == "unauthorized"

    def test_wrong_token_is_refused(self, client: TestClient) -> None:
        response = client.post(
            "/internal/scheduler/check-all", headers={"X-Internal-Token": "wrong"}
        )
        assert response.status_code == 401

    def test_unconfigured_token_refuses_even_a_blank_header(
        self, dummy_env: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Unlike X-API-Key, there is no "unset means open" case here."""
        from product_tracker.core.config import reset_settings_cache

        reset_settings_cache()
        client = TestClient(create_app(), raise_server_exceptions=False)

        response = client.post("/internal/scheduler/check-all")
        assert response.status_code == 401


class TestSuccessfulSweep:
    def test_summary_is_reported(self, client: TestClient) -> None:
        summary = CheckAllSummary(
            outcomes=[_outcome(1), _outcome(2)],
            skipped=3,
            delivery=DeliveryReport(created=2, sent=2, failed=0, suppressed=0),
        )
        with patch(
            "product_tracker.api.routers.internal.run_all_checks", return_value=summary
        ) as mock_run:
            response = client.post(
                "/internal/scheduler/check-all", headers={"X-Internal-Token": TOKEN}
            )

        assert response.status_code == 200
        assert response.json() == {
            "status": "completed",
            "checked": 2,
            "skipped": 3,
            "failures": 0,
            "notifications_sent": 2,
            "notifications_failed": 0,
        }
        mock_run.assert_called_once()
        assert mock_run.call_args.kwargs["limit"] == 100

    def test_limit_is_forwarded(self, client: TestClient) -> None:
        empty = DeliveryReport(created=0, sent=0, failed=0, suppressed=0)
        summary = CheckAllSummary(outcomes=[], skipped=0, delivery=empty)
        with patch(
            "product_tracker.api.routers.internal.run_all_checks", return_value=summary
        ) as mock_run:
            response = client.post(
                "/internal/scheduler/check-all?limit=5",
                headers={"X-Internal-Token": TOKEN},
            )

        assert response.status_code == 200
        assert mock_run.call_args.kwargs["limit"] == 5

    def test_limit_is_bounded(self, client: TestClient) -> None:
        response = client.post(
            "/internal/scheduler/check-all?limit=0", headers={"X-Internal-Token": TOKEN}
        )
        assert response.status_code == 422
