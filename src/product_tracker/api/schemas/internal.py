"""Schema for the internal scheduler trigger."""

from __future__ import annotations

from pydantic import BaseModel, Field


class CheckAllResponse(BaseModel):
    """Summary of one bulk sweep. Not a per-product report -- see ``/products/{id}/check``
    or ``check_executions`` for that; this exists for a cron job to log one line."""

    status: str = Field(description="Always 'completed'. A failed sweep is an HTTP error.")
    checked: int = Field(description="Products actually checked in this sweep.")
    skipped: int = Field(
        description="Products already claimed elsewhere (a live worker, or an overlapping "
        "sweep) and left untouched."
    )
    failures: int = Field(description="Of those checked, how many recorded a failed status.")
    notifications_sent: int
    notifications_failed: int
