"""Alert CLI commands, plus pause and resume."""

from __future__ import annotations

from collections.abc import Iterator

import httpx
import pytest
import respx
from tests.unit.test_adapters import load
from typer.testing import CliRunner

from product_tracker.cli.formatting import ExitCode
from product_tracker.cli.main import app

pytestmark = pytest.mark.db

runner = CliRunner()
URL = "https://shop.example.com/p/cli-alerts"


@pytest.fixture(autouse=True)
def _respx_router() -> Iterator[None]:
    with respx.mock:
        yield


def add_product(html: str | None = None) -> None:
    respx.get(URL).mock(
        return_value=httpx.Response(200, html=html or load("jsonld_in_stock.html"))
    )
    runner.invoke(app, ["add", URL, "--no-check"])


class TestAlertsAdd:
    def test_adds_a_rule(self, clean_db: None) -> None:
        add_product()

        result = runner.invoke(app, ["alerts", "add", "1", "--type", "price_dropped"])

        assert result.exit_code == ExitCode.OK
        assert "alert 1 added" in result.stdout

    def test_target_price_rule(self, clean_db: None) -> None:
        add_product()

        result = runner.invoke(
            app, ["alerts", "add", "1", "--type", "price_below_target", "--target", "69999"]
        )

        assert result.exit_code == ExitCode.OK

    def test_target_is_required_for_that_rule(self, clean_db: None) -> None:
        add_product()

        result = runner.invoke(app, ["alerts", "add", "1", "--type", "price_below_target"])

        assert result.exit_code == ExitCode.ERROR
        assert "target_price" in result.stdout + result.stderr

    def test_duplicate_rule_type_is_refused(self, clean_db: None) -> None:
        add_product()
        runner.invoke(app, ["alerts", "add", "1", "--type", "price_dropped"])

        result = runner.invoke(app, ["alerts", "add", "1", "--type", "price_dropped"])

        assert result.exit_code == ExitCode.ERROR
        assert "already exists" in result.stdout + result.stderr

    def test_missing_product_exits_not_found(self, clean_db: None) -> None:
        result = runner.invoke(app, ["alerts", "add", "999999", "--type", "price_dropped"])
        assert result.exit_code == ExitCode.NOT_FOUND

    def test_unknown_provider_is_refused(self, clean_db: None) -> None:
        add_product()

        result = runner.invoke(
            app,
            ["alerts", "add", "1", "--type", "price_dropped", "--provider", "carrier-pigeon"],
        )

        assert result.exit_code == ExitCode.ERROR


class TestAlertsList:
    def test_empty_is_explained(self, clean_db: None) -> None:
        result = runner.invoke(app, ["alerts", "list"])

        assert result.exit_code == ExitCode.OK
        assert "no alerts configured" in result.stdout + result.stderr

    def test_shows_configured_alerts(self, clean_db: None) -> None:
        add_product()
        runner.invoke(
            app, ["alerts", "add", "1", "--type", "price_below_target", "--target", "69999"]
        )

        result = runner.invoke(app, ["alerts", "list"])

        assert result.exit_code == ExitCode.OK
        assert "price_below_target" in result.stdout
        assert "69999" in result.stdout

    def test_filters_by_product(self, clean_db: None) -> None:
        add_product()
        runner.invoke(app, ["alerts", "add", "1", "--type", "price_dropped"])

        result = runner.invoke(app, ["alerts", "list", "--product", "1"])

        assert result.exit_code == ExitCode.OK
        assert "price_dropped" in result.stdout


class TestAlertsRemove:
    def test_removes(self, clean_db: None) -> None:
        add_product()
        runner.invoke(app, ["alerts", "add", "1", "--type", "price_dropped"])

        result = runner.invoke(app, ["alerts", "remove", "1", "--yes"])

        assert result.exit_code == ExitCode.OK
        assert "no alerts configured" in (
            runner.invoke(app, ["alerts", "list"]).stdout
            + runner.invoke(app, ["alerts", "list"]).stderr
        )

    def test_missing_exits_not_found(self, clean_db: None) -> None:
        assert runner.invoke(app, ["alerts", "remove", "999999", "--yes"]).exit_code == (
            ExitCode.NOT_FOUND
        )


class TestAlertsSetEnabled:
    def test_off_then_on(self, clean_db: None) -> None:
        add_product()
        runner.invoke(app, ["alerts", "add", "1", "--type", "price_dropped"])

        off = runner.invoke(app, ["alerts", "set-enabled", "1", "--off"])
        assert off.exit_code == ExitCode.OK
        assert "is now disabled" in off.stdout

        on = runner.invoke(app, ["alerts", "set-enabled", "1", "--on"])
        assert on.exit_code == ExitCode.OK
        assert "is now enabled" in on.stdout

    def test_defaults_to_on(self, clean_db: None) -> None:
        add_product()
        runner.invoke(app, ["alerts", "add", "1", "--type", "price_dropped"])
        runner.invoke(app, ["alerts", "set-enabled", "1", "--off"])

        result = runner.invoke(app, ["alerts", "set-enabled", "1"])

        assert result.exit_code == ExitCode.OK
        assert "is now enabled" in result.stdout

    def test_missing_rule_exits_not_found(self, clean_db: None) -> None:
        assert runner.invoke(
            app, ["alerts", "set-enabled", "999999", "--off"]
        ).exit_code == ExitCode.NOT_FOUND


class TestPauseResume:
    def test_pause_then_resume(self, clean_db: None) -> None:
        add_product()

        paused = runner.invoke(app, ["pause", "1"])
        assert paused.exit_code == ExitCode.OK
        assert "paused" in runner.invoke(app, ["show", "1"]).stdout

        resumed = runner.invoke(app, ["resume", "1"])
        assert resumed.exit_code == ExitCode.OK
        assert "active" in runner.invoke(app, ["show", "1"]).stdout

    def test_pause_missing_exits_not_found(self, clean_db: None) -> None:
        assert runner.invoke(app, ["pause", "999999"]).exit_code == ExitCode.NOT_FOUND


class TestAlertHistory:
    def test_shows_generated_notifications(self, clean_db: None) -> None:
        add_product()
        runner.invoke(
            app, ["alerts", "add", "1", "--type", "price_below_target", "--target", "70000"]
        )
        runner.invoke(app, ["check", "1"])

        result = runner.invoke(app, ["alerts", "history", "1"])

        assert result.exit_code == ExitCode.OK
        assert "price_below_target" in result.stdout

    def test_empty_is_explained(self, clean_db: None) -> None:
        add_product()

        result = runner.invoke(app, ["alerts", "history", "1"])

        assert "no notifications" in result.stdout + result.stderr


class TestEndToEnd:
    def test_a_price_drop_produces_exactly_one_alert(self, clean_db: None) -> None:
        """The whole path: rule, check, change, notification -- and no duplicate."""
        add_product()
        runner.invoke(app, ["alerts", "add", "1", "--type", "price_dropped"])
        runner.invoke(app, ["check", "1"])  # baseline

        respx.get(URL).mock(
            return_value=httpx.Response(
                200, html=load("jsonld_in_stock.html").replace("69999.00", "59999.00")
            )
        )
        runner.invoke(app, ["check", "1"])
        runner.invoke(app, ["check", "1"])  # same price again

        history = runner.invoke(app, ["alerts", "history", "1"]).stdout
        assert history.count("price_dropped") == 1
