"""Declarative base and shared column helpers.

An explicit naming convention is set so that indexes and constraints get deterministic
names. Without it, Alembic autogenerate emits unnamed constraints that later migrations
cannot drop portably.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, MetaData, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

NAMING_CONVENTION: dict[str, str] = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    """Base class for every ORM model."""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)

    def __repr__(self) -> str:
        identifier = getattr(self, "id", None)
        return f"<{type(self).__name__} id={identifier}>"


def utc_now_column(**kwargs: Any) -> Mapped[datetime]:
    """A timezone-aware timestamp defaulting to the database's ``now()``."""
    return mapped_column(DateTime(timezone=True), server_default=func.now(), **kwargs)
