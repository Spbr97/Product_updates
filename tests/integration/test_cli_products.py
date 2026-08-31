"""Product CLI commands and their exit codes."""

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
URL = "https://shop.example.com/p/cli-1"


def stub_ok(url: str) -> None:
    respx.get(url).mock(return_value=httpx.Response(200, html=load("jsonld_in_stock.html")))


@respx.mock
class TestAdd:
    def test_adds_and_checks_in_one_step(self, clean_db: None) -> None:
        stub_ok(URL)

        result = runner.invoke(app, ["add", URL])

        assert result.exit_code == ExitCode.OK
        assert "tracking product" in result.stdout
        assert "69,999.00" in result.stdout

    def test_no_check_skips_the_fetch(self, clean_db: None) -> None:
        result = runner.invoke(app, ["add", URL, "--no-check"])

        assert result.exit_code == ExitCode.OK
        assert "Check result" not in result.stdout

    def test_duplicate_exits_with_error(self, clean_db: None) -> None:
        runner.invoke(app, ["add", URL, "--no-check"])

        result = runner.invoke(app, ["add", URL, "--no-check"])

        assert result.exit_code == ExitCode.ERROR
        assert "already tracked" in result.stdout + result.stderr

    def test_invalid_url_exits_with_error(self, clean_db: None) -> None:
        result = runner.invoke(app, ["add", "ftp://example.com/x", "--no-check"])
        assert result.exit_code == ExitCode.ERROR

    def test_ssrf_attempt_is_refused(
        self, clean_db: None, strict_url_policy: None
    ) -> None:
        result = runner.invoke(app, ["add", "http://127.0.0.1/admin", "--no-check"])

        assert result.exit_code == ExitCode.ERROR
        assert "non-public" in result.stdout + result.stderr


@respx.mock
class TestListShowRemove:
    def test_list_is_helpful_when_empty(self, clean_db: None) -> None:
        result = runner.invoke(app, ["list"])

        assert result.exit_code == ExitCode.OK
        assert "no products tracked yet" in result.stdout + result.stderr

    def test_list_shows_added_products(self, clean_db: None) -> None:
        runner.invoke(app, ["add", URL, "--no-check"])

        result = runner.invoke(app, ["list"])

        assert result.exit_code == ExitCode.OK
        assert "generic" in result.stdout

    def test_show_displays_detail(self, clean_db: None) -> None:
        stub_ok(URL)
        runner.invoke(app, ["add", URL])

        result = runner.invoke(app, ["show", "1"])

        assert result.exit_code == ExitCode.OK
        assert "Recent checks" in result.stdout

    def test_show_missing_exits_not_found(self, clean_db: None) -> None:
        result = runner.invoke(app, ["show", "999999"])
        assert result.exit_code == ExitCode.NOT_FOUND

    def test_remove_missing_exits_not_found(self, clean_db: None) -> None:
        result = runner.invoke(app, ["remove", "999999", "--yes"])
        assert result.exit_code == ExitCode.NOT_FOUND

    def test_remove_deletes(self, clean_db: None) -> None:
        runner.invoke(app, ["add", URL, "--no-check"])

        removed = runner.invoke(app, ["remove", "1", "--yes"])

        assert removed.exit_code == ExitCode.OK
        assert runner.invoke(app, ["show", "1"]).exit_code == ExitCode.NOT_FOUND

    def test_remove_without_yes_aborts_on_no(self, clean_db: None) -> None:
        """Deleting cascades to price history, so it must confirm first."""
        runner.invoke(app, ["add", URL, "--no-check"])

        result = runner.invoke(app, ["remove", "1"], input="n\n")

        assert result.exit_code != ExitCode.OK
        assert runner.invoke(app, ["show", "1"]).exit_code == ExitCode.OK


@respx.mock
class TestCheck:
    def test_successful_check_exits_zero(self, clean_db: None) -> None:
        stub_ok(URL)
        runner.invoke(app, ["add", URL, "--no-check"])

        result = runner.invoke(app, ["check", "1"])

        assert result.exit_code == ExitCode.OK
        assert "in_stock" in result.stdout

    def test_store_failure_exits_with_store_failure_code(self, clean_db: None) -> None:
        """Distinct from a crash, so scripts can retry rather than investigate."""
        respx.get(URL).mock(return_value=httpx.Response(403))
        runner.invoke(app, ["add", URL, "--no-check"])

        result = runner.invoke(app, ["check", "1"])

        assert result.exit_code == ExitCode.STORE_FAILURE

    def test_partial_check_is_not_a_failure_exit(self, clean_db: None) -> None:
        """Price missing but page readable: worth reporting, not worth a failure code."""
        url = "https://www.flipkart.com/p/itm-cli-noprice"
        respx.get(url).mock(
            return_value=httpx.Response(200, html=load("flipkart_no_price.html"))
        )
        runner.invoke(app, ["add", url, "--no-check"])

        result = runner.invoke(app, ["check", "1"])

        assert result.exit_code == ExitCode.OK
        assert "partial" in result.stdout

    def test_check_missing_exits_not_found(self, clean_db: None) -> None:
        result = runner.invoke(app, ["check", "999999"])
        assert result.exit_code == ExitCode.NOT_FOUND
