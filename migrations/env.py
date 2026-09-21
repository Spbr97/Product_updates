"""Alembic environment.

The database URL comes from application settings (``DATABASE_URL``), never from
``alembic.ini``, so migrations and the application can never disagree about which database
they are pointed at, and no credential is committed.
"""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from product_tracker.core.config import get_settings
from product_tracker.db import models as _models  # noqa: F401  (registers tables on Base)
from product_tracker.db.base import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata

# ConfigParser (which config.set_main_option writes into) treats a bare "%" as its own
# interpolation syntax, so a percent-encoded password (e.g. "%40" for a literal "@") would
# otherwise raise "invalid interpolation syntax" here. Doubling it survives the round trip:
# get_main_option decodes "%%" back to "%" on read, same as ConfigParser's own escaping.
config.set_main_option("sqlalchemy.url", get_settings().database_url.replace("%", "%%"))


def run_migrations_offline() -> None:
    """Emit SQL to stdout without connecting (``alembic upgrade head --sql``)."""
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations against a live connection."""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            compare_server_default=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
