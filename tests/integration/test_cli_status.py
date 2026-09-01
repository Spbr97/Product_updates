"""The enriched ``status`` command, and exit-code coverage across the CLI."""

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
URL = "https://shop.example.com/p/status"


@pytest.fixture(autouse=True)
def _respx_router() -> Iterator[None]:
    with respx.mock:
        yield


class TestStatus:
    def test_reports_every_section(self, clean_db: None) -> None:
        result = runner.invoke(app, ["status"])

        assert result.exit_code == ExitCode.OK
        for section in ("Configuration", "State", "Worker", "Notifications"):
            assert section in result.stdout

    def test_reports_that_no_worker_has_started(self, clean_db: None) -> None:
        """Liveness comes from the heartbeat now, not from guessing at the job store."""
        result = runner.invoke(app, ["status"])

        assert "never started" in result.stdout
        assert "has ever reported in" in result.stdout

    def test_reports_a_live_worker(self, clean_db: None) -> None:
        from product_tracker.db.session import session_scope
        from product_tracker.scheduler import heartbeat

        with session_scope() as session:
            heartbeat.touch(session, "test-worker")

        result = runner.invoke(app, ["status"])

        assert "running" in result.stdout
        assert "last beat" in result.stdout

    def test_shows_recent_check_counts(self, clean_db: None) -> None:
        respx.get(URL).mock(return_value=httpx.Response(200, html=load("jsonld_in_stock.html")))
        runner.invoke(app, ["add", URL])

        result = runner.invoke(app, ["status"])

        assert "Checks (last 24h)" in result.stdout
        assert "success" in result.stdout

    def test_lists_provider_configuration(self, clean_db: None) -> None:
        result = runner.invoke(app, ["status"])

        assert "console" in result.stdout
        assert "telegram" in result.stdout

    def test_masks_the_database_password(self, clean_db: None) -> None:
        assert "tracker:tracker@" not in runner.invoke(app, ["status"]).stdout


class TestConfig:
    def test_shows_the_new_settings(self, clean_db: None) -> None:
        result = runner.invoke(app, ["config"])

        assert result.exit_code == ExitCode.OK
        assert "api_max_request_bytes" in result.stdout

    def test_redacts_the_api_key(
        self, clean_db: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from product_tracker.core.config import reset_settings_cache

        monkeypatch.setenv("API_KEY", "s3cret-api-key")
        reset_settings_cache()

        result = runner.invoke(app, ["config"])

        assert "s3cret-api-key" not in result.stdout
        assert "***set***" in result.stdout


class TestWorkerCommands:
    def test_dry_run_with_no_products(self, clean_db: None) -> None:
        result = runner.invoke(app, ["worker", "--dry-run"])

        assert result.exit_code == ExitCode.OK
        assert "no active products" in result.stdout + result.stderr

    def test_dry_run_lists_what_would_be_scheduled(self, clean_db: None) -> None:
        runner.invoke(app, ["add", URL, "--no-check"])

        result = runner.invoke(app, ["worker", "--dry-run"])

        assert result.exit_code == ExitCode.OK
        assert "Would schedule" in result.stdout

    def test_dry_run_excludes_paused_products(self, clean_db: None) -> None:
        runner.invoke(app, ["add", URL, "--no-check"])
        runner.invoke(app, ["pause", "1"])

        result = runner.invoke(app, ["worker", "--dry-run"])

        assert "no active products" in result.stdout + result.stderr

    def test_check_all_with_no_products(self, clean_db: None) -> None:
        result = runner.invoke(app, ["check-all"])

        assert result.exit_code == ExitCode.OK
        assert "no active products" in result.stdout + result.stderr

    def test_check_all_checks_each_product(self, clean_db: None) -> None:
        respx.get(URL).mock(return_value=httpx.Response(200, html=load("jsonld_in_stock.html")))
        runner.invoke(app, ["add", URL, "--no-check"])

        result = runner.invoke(app, ["check-all"])

        assert result.exit_code == ExitCode.OK
        assert "success" in result.stdout

    def test_check_all_reports_store_failure(self, clean_db: None) -> None:
        respx.get(URL).mock(return_value=httpx.Response(403))
        runner.invoke(app, ["add", URL, "--no-check"])

        result = runner.invoke(app, ["check-all"])

        assert result.exit_code == ExitCode.STORE_FAILURE


class TestExitCodes:
    """Every documented code is reachable, so scripts can rely on them."""

    def test_ok(self, clean_db: None) -> None:
        assert runner.invoke(app, ["status"]).exit_code == ExitCode.OK

    def test_not_found(self, clean_db: None) -> None:
        assert runner.invoke(app, ["show", "999999"]).exit_code == ExitCode.NOT_FOUND

    def test_store_failure(self, clean_db: None) -> None:
        respx.get(URL).mock(return_value=httpx.Response(403))
        runner.invoke(app, ["add", URL, "--no-check"])

        assert runner.invoke(app, ["check", "1"]).exit_code == ExitCode.STORE_FAILURE

    def test_error(self, clean_db: None) -> None:
        assert runner.invoke(app, ["add", "not-a-url"]).exit_code == ExitCode.ERROR

    def test_config_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from product_tracker.core.config import reset_settings_cache

        monkeypatch.delenv("DATABASE_URL", raising=False)
        reset_settings_cache()

        assert runner.invoke(app, ["status"]).exit_code == ExitCode.CONFIG_ERROR


class TestHelp:
    @pytest.mark.parametrize(
        "command",
        [
            "add", "list", "show", "remove", "check", "history",
            "pause", "resume", "worker", "check-all", "status", "config",
        ],
    )
    def test_every_command_has_help(self, command: str) -> None:
        result = runner.invoke(app, [command, "--help"])

        assert result.exit_code == ExitCode.OK
        assert command.split("-")[0] in result.stdout.lower() or "Usage" in result.stdout

    def test_alerts_subcommands_have_help(self) -> None:
        assert runner.invoke(app, ["alerts", "--help"]).exit_code == ExitCode.OK
        for sub in ("add", "list", "remove", "history"):
            assert runner.invoke(app, ["alerts", sub, "--help"]).exit_code == ExitCode.OK
