"""Product CLI commands and their exit codes."""

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
URL = "https://shop.example.com/p/cli-1"


@pytest.fixture(autouse=True)
def _respx_router() -> Iterator[None]:
    """Activate respx for every test in this module.

    Not ``@respx.mock`` on the class: in respx 0.23 that decorator returns a *function*,
    so pytest silently stops collecting the class and the tests never run.
    """
    with respx.mock:
        yield


def stub_ok(url: str) -> None:
    respx.get(url).mock(return_value=httpx.Response(200, html=load("jsonld_in_stock.html")))


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


class TestCheckIsScopedToItsOwner:
    """`check` was the one product command that acted on any listing by id.

    Every sibling -- show, pause, resume, remove -- resolves an account and asserts a
    subscription. `check` called run_check directly, which made it both a way to read a
    price you do not watch and a way to make this deployment fetch from a retailer on
    demand for someone else's listing. The API route was hardened for exactly that reason;
    the CLI kept the hole until a live acceptance run walked into it.
    """

    def test_another_account_cannot_check_your_listing(self, clean_db: None) -> None:
        from product_tracker.db.session import session_scope
        from product_tracker.services import user_service

        stub_ok(URL)
        runner.invoke(app, ["add", URL])  # tracked by the default account
        with session_scope() as session:
            user_service.create_user(session, email="stranger@example.com")

        result = runner.invoke(app, ["check", "1", "--user", "stranger@example.com"])

        # Not found rather than forbidden: a 403 would confirm the id exists.
        assert result.exit_code == ExitCode.NOT_FOUND

    def test_the_owner_still_can(self, clean_db: None) -> None:
        stub_ok(URL)
        runner.invoke(app, ["add", URL])
        assert runner.invoke(app, ["check", "1"]).exit_code == ExitCode.OK

    def test_adding_as_a_named_user_still_checks(self, clean_db: None) -> None:
        """Regression: `add` hands the product to `check`, and scoping it meant the
        follow-up check ran as the wrong account unless the user came with it."""
        from product_tracker.db.session import session_scope
        from product_tracker.services import user_service

        with session_scope() as session:
            user_service.create_user(session, email="owner@example.com")
        stub_ok(URL)

        result = runner.invoke(app, ["add", URL, "--user", "owner@example.com"])

        assert result.exit_code == ExitCode.OK
        assert "status" in result.stdout  # the check ran and printed its result table
