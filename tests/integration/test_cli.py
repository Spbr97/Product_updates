"""CLI smoke tests.

The version/help paths must work with no configuration at all -- that is what a user hits
first. The rest need the database.
"""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from product_tracker import __version__
from product_tracker.cli.formatting import ExitCode
from product_tracker.cli.main import app

runner = CliRunner()


class TestWithoutConfiguration:
    def test_version_works_unconfigured(self) -> None:
        """No DATABASE_URL set; --version must still answer."""
        result = runner.invoke(app, ["--version"])

        assert result.exit_code == ExitCode.OK
        assert __version__ in result.stdout

    def test_help_works_unconfigured(self) -> None:
        result = runner.invoke(app, ["--help"])

        assert result.exit_code == ExitCode.OK
        assert "status" in result.stdout

    def test_status_reports_config_error(self) -> None:
        """Missing settings exit with CONFIG_ERROR, not a traceback."""
        result = runner.invoke(app, ["status"])

        assert result.exit_code == ExitCode.CONFIG_ERROR

    def test_config_reports_config_error(self) -> None:
        assert runner.invoke(app, ["config"]).exit_code == ExitCode.CONFIG_ERROR


class TestConfigCommand:
    def test_config_redacts_secrets(self, dummy_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
        from product_tracker.core.config import reset_settings_cache

        monkeypatch.setenv("SMTP_PASSWORD", "hunter2")
        reset_settings_cache()

        result = runner.invoke(app, ["config"])

        assert result.exit_code == ExitCode.OK
        assert "hunter2" not in result.stdout
        assert "***set***" in result.stdout


@pytest.mark.db
class TestAgainstDatabase:
    def test_status_succeeds(self, db_env: None) -> None:
        result = runner.invoke(app, ["status"])

        assert result.exit_code == ExitCode.OK
        assert "migration revision" in result.stdout

    def test_status_masks_the_database_password(self, db_env: None) -> None:
        result = runner.invoke(app, ["status"])
        assert "tracker:tracker@" not in result.stdout

    def test_stores_list_shows_seeded_stores(self, db_env: None) -> None:
        result = runner.invoke(app, ["stores", "list"])

        assert result.exit_code == ExitCode.OK
        assert "generic" in result.stdout

    def test_stores_sync_is_idempotent(self, db_env: None) -> None:
        first = runner.invoke(app, ["stores", "sync"])
        second = runner.invoke(app, ["stores", "sync"])

        assert first.exit_code == ExitCode.OK
        assert second.exit_code == ExitCode.OK
        assert "0 created, 0 updated" in second.stdout
