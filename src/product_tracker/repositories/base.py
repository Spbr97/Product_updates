"""Repository base.

Repositories are thin: they translate between the service layer and SQLAlchemy, and hold
no business rules. They never commit -- the caller owns the transaction via
``session_scope`` -- so a service can group several repository calls into one unit of work.
"""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..db.base import Base


class Repository[ModelT: Base]:
    """Common lookups shared by every repository."""

    model: type[ModelT]

    def __init__(self, session: Session) -> None:
        self.session = session

    def get(self, entity_id: int) -> ModelT | None:
        return self.session.get(self.model, entity_id)

    def count(self) -> int:
        stmt = select(func.count()).select_from(self.model)
        return int(self.session.execute(stmt).scalar_one())

    def add(self, entity: ModelT) -> ModelT:
        """Stage an insert and flush so the caller sees the generated primary key."""
        self.session.add(entity)
        self.session.flush()
        return entity

    def delete(self, entity: ModelT) -> None:
        self.session.delete(entity)
        self.session.flush()
