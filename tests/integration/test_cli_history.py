"""The ``history`` CLI command."""

from __future__ import annotations

import httpx
import pytest
import respx
from tests.unit.test_adapters import load
from typer.testing import CliRunner

from product_tracker.cli.formatting import ExitCode
from product_tracker.cli.main import app

pytestmark = pytest.mark.db

runner = CliRunner()
URL = "https://shop.example.com/p/cli-history"


def add_and_check(html: str | None = None) -> None:
    respx.get(URL).mock(
        return_value=httpx.Response(200, html=html or load("jsonld_in_stock.html"))
    )
    runner.invoke(app, ["add", URL])


@respx.mock
class TestHistory:
    def test_shows_recorded_prices(self, clean_db: None) -> None:
        add_and_check()

        result = runner.invoke(app, ["history", "1"])

        assert result.exit_code == ExitCode.OK
        assert "69,999.00" in result.stdout

    def test_empty_history_is_explained_not_an_error(self, clean_db: None) -> None:
        runner.invoke(app, ["add", URL, "--no-check"])

        result = runner.invoke(app, ["history", "1"])

        assert result.exit_code == ExitCode.OK
        assert "no price history yet" in result.stdout + result.stderr

    def test_missing_product_exits_not_found(self, clean_db: None) -> None:
        assert runner.invoke(app, ["history", "999999"]).exit_code == ExitCode.NOT_FOUND

    def test_shows_a_price_drop(self, clean_db: None) -> None:
        add_and_check()
        respx.get(URL).mock(
            return_value=httpx.Response(
                200, html=load("jsonld_in_stock.html").replace("69999.00", "59999.00")
            )
        )
        runner.invoke(app, ["check", "1"])

        result = runner.invoke(app, ["history", "1"])

        assert result.exit_code == ExitCode.OK
        assert "59,999.00" in result.stdout
        assert "69,999.00" in result.stdout


@respx.mock
class TestStatsFlag:
    def test_reports_statistics(self, clean_db: None) -> None:
        add_and_check()

        result = runner.invoke(app, ["history", "1", "--stats"])

        assert result.exit_code == ExitCode.OK
        assert "observations" in result.stdout
        assert "lowest" in result.stdout
        assert "69,999.00" in result.stdout

    def test_reports_the_drop(self, clean_db: None) -> None:
        add_and_check()
        respx.get(URL).mock(
            return_value=httpx.Response(
                200, html=load("jsonld_in_stock.html").replace("69999.00", "59999.00")
            )
        )
        runner.invoke(app, ["check", "1"])

        result = runner.invoke(app, ["history", "1", "--stats"])

        assert "-14.29%" in result.stdout

    def test_without_history_it_says_so(self, clean_db: None) -> None:
        runner.invoke(app, ["add", URL, "--no-check"])

        result = runner.invoke(app, ["history", "1", "--stats"])

        assert result.exit_code == ExitCode.OK
        assert "no price history yet" in result.stdout + result.stderr


@respx.mock
class TestAvailabilityFlag:
    def test_shows_availability_history(self, clean_db: None) -> None:
        add_and_check()

        result = runner.invoke(app, ["history", "1", "--availability"])

        assert result.exit_code == ExitCode.OK
        assert "in_stock" in result.stdout

    def test_shows_a_transition(self, clean_db: None) -> None:
        add_and_check()
        respx.get(URL).mock(
            return_value=httpx.Response(200, html=load("jsonld_out_of_stock.html"))
        )
        runner.invoke(app, ["check", "1"])

        result = runner.invoke(app, ["history", "1", "--availability"])

        assert "out_of_stock" in result.stdout
        assert "in_stock" in result.stdout
