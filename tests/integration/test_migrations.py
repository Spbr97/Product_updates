"""Migrations produce the expected schema and can be rolled back.

Requires PostgreSQL: see ``TEST_DATABASE_URL`` in .env.example.
"""

from __future__ import annotations

import pytest
from sqlalchemy import inspect, text

from product_tracker.db.session import get_engine

pytestmark = pytest.mark.db

EXPECTED_TABLES = {
    "stores",
    "products",
    "price_history",
    "availability_history",
    "tracking_rules",
    "notifications",
    "check_executions",
}

EXPECTED_ENUMS = {
    "availability",
    "tracking_status",
    "rule_type",
    "check_status",
    "fetch_method",
    "notification_status",
}


class TestSchema:
    def test_all_tables_exist(self, db_env: None) -> None:
        tables = set(inspect(get_engine()).get_table_names())
        assert tables >= EXPECTED_TABLES

    def test_enum_types_created_once(self, db_env: None) -> None:
        """``availability`` is shared by three tables; it must exist exactly once."""
        with get_engine().connect() as connection:
            rows = connection.execute(
                text("SELECT typname, COUNT(*) FROM pg_type WHERE typtype = 'e' GROUP BY typname")
            ).all()
        counts = dict(rows)
        assert set(counts) >= EXPECTED_ENUMS
        assert counts["availability"] == 1

    def test_canonical_url_is_unique(self, db_env: None) -> None:
        """Duplicate detection depends on this constraint existing."""
        constraints = inspect(get_engine()).get_unique_constraints("products")
        assert any(c["column_names"] == ["url_canonical"] for c in constraints)

    def test_dedupe_key_is_unique(self, db_env: None) -> None:
        """Notification idempotency depends on this constraint existing."""
        constraints = inspect(get_engine()).get_unique_constraints("notifications")
        assert any(c["column_names"] == ["dedupe_key"] for c in constraints)

    @pytest.mark.parametrize(
        ("table", "columns"),
        [
            ("price_history", ["product_id", "observed_at"]),
            ("availability_history", ["product_id", "observed_at"]),
            ("check_executions", ["product_id", "started_at"]),
            ("notifications", ["product_id", "created_at"]),
        ],
    )
    def test_history_lookup_indexes_exist(
        self, db_env: None, table: str, columns: list[str]
    ) -> None:
        """History reads are always 'latest rows for this product'."""
        indexes = inspect(get_engine()).get_indexes(table)
        assert any(index["column_names"] == columns for index in indexes), (
            f"{table} is missing an index on {columns}"
        )


class TestSeedData:
    def test_stores_are_seeded(self, db_env: None) -> None:
        with get_engine().connect() as connection:
            slugs = set(connection.execute(text("SELECT slug FROM stores")).scalars())
        assert {"generic", "flipkart"} <= slugs

    def test_seed_is_idempotent(self, db_env: None) -> None:
        """Re-running the seed insert must not duplicate rows.

        The migration uses ON CONFLICT DO NOTHING so it is safe on a database where
        ``stores sync`` already created the rows.
        """
        insert = text(
            "INSERT INTO stores (slug, name, domains, adapter_key, enabled) "
            "VALUES ('generic', 'Generic (schema.org)', CAST('[]' AS jsonb), 'generic', true) "
            "ON CONFLICT (slug) DO NOTHING"
        )
        with get_engine().begin() as connection:
            before = connection.execute(
                text("SELECT COUNT(*) FROM stores WHERE slug = 'generic'")
            ).scalar_one()
            connection.execute(insert)
            after = connection.execute(
                text("SELECT COUNT(*) FROM stores WHERE slug = 'generic'")
            ).scalar_one()

        assert before == after == 1


class TestRollback:
    def test_downgrade_then_upgrade_is_clean(self, db_env: None) -> None:
        """A full down/up cycle must leave the schema exactly as it was.

        This catches enum types that are created but never dropped -- the second upgrade
        would fail on 'type already exists'.
        """
        from alembic import command
        from alembic.config import Config
        from tests.conftest import PROJECT_ROOT

        config = Config(str(PROJECT_ROOT / "alembic.ini"))
        config.set_main_option("script_location", str(PROJECT_ROOT / "migrations"))

        command.downgrade(config, "base")
        with get_engine().connect() as connection:
            remaining = set(inspect(connection).get_table_names())
        assert not (EXPECTED_TABLES & remaining)

        command.upgrade(config, "head")
        with get_engine().connect() as connection:
            restored = set(inspect(connection).get_table_names())
        assert restored >= EXPECTED_TABLES
