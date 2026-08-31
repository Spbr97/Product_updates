"""Check-execution repository -- the diagnostic trail."""

from __future__ import annotations

from sqlalchemy import desc, select

from ..db.models import CheckExecution
from .base import Repository

#: Error details are truncated before storage. A stack trace or a page fragment would
#: bloat the row and risks capturing content we do not want to retain.
MAX_ERROR_DETAIL = 1000


class CheckExecutionRepository(Repository[CheckExecution]):
    model = CheckExecution

    def list_for_product(self, product_id: int, *, limit: int = 20) -> list[CheckExecution]:
        stmt = (
            select(CheckExecution)
            .where(CheckExecution.product_id == product_id)
            .order_by(desc(CheckExecution.started_at), desc(CheckExecution.id))
            .limit(limit)
        )
        return list(self.session.execute(stmt).scalars())

    def latest_for_product(self, product_id: int) -> CheckExecution | None:
        rows = self.list_for_product(product_id, limit=1)
        return rows[0] if rows else None


def truncate_error(detail: str | None) -> str | None:
    """Clamp an error message to a storable length."""
    if detail is None:
        return None
    text = detail.strip()
    if not text:
        return None
    if len(text) <= MAX_ERROR_DETAIL:
        return text
    return text[: MAX_ERROR_DETAIL - 3] + "..."
