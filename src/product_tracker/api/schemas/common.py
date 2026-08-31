"""Shared API schemas: the error envelope, pagination, and health payloads."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ErrorBody(BaseModel):
    """The body of every error response."""

    type: str = Field(description="Stable machine-readable error code.")
    message: str = Field(description="Human-readable summary. Safe to display.")
    detail: dict[str, Any] | None = Field(
        default=None, description="Optional structured context (e.g. field errors)."
    )


class ErrorResponse(BaseModel):
    """Every non-2xx response has this shape, so clients parse errors one way."""

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "error": {
                    "type": "not_found",
                    "message": "Product 42 not found",
                    "detail": None,
                }
            }
        }
    )

    error: ErrorBody


class Page[ItemT](BaseModel):
    """An offset-paginated slice of a collection."""

    items: list[ItemT]
    total: int = Field(description="Total rows matching the filter, ignoring pagination.")
    limit: int
    offset: int

    @property
    def has_more(self) -> bool:
        return self.offset + len(self.items) < self.total


class HealthResponse(BaseModel):
    """Liveness. Answers without touching any dependency."""

    status: str = "ok"
    version: str
    service: str = "product-tracker"


class DependencyStatus(BaseModel):
    name: str
    healthy: bool
    detail: str | None = None


class ReadinessResponse(BaseModel):
    """Readiness. Reports each dependency the process needs to serve traffic."""

    status: str = Field(description="'ready' when every required dependency is healthy.")
    version: str
    dependencies: list[DependencyStatus]
