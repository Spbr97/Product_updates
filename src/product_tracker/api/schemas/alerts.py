"""Alert request and response models."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from ...domain.enums import RuleType


class AlertCreate(BaseModel):
    """Request body for creating a tracking rule.

    ``target_price`` is promoted to a first-class field because it is the only parameter
    any current rule takes; ``params`` stays open for conditions added later, which is the
    point of storing rule settings as JSONB.
    """

    product_id: int
    rule_type: RuleType
    target_price: Decimal | None = Field(
        default=None,
        gt=0,
        description="Required for price_below_target. Ignored by other rule types.",
    )
    params: dict[str, Any] = Field(
        default_factory=dict, description="Additional rule-specific settings."
    )
    notify_provider: str | None = Field(
        default=None,
        description="Deliver through one channel only. Omit to use every configured one.",
    )
    cooldown_seconds: int | None = Field(
        default=None, ge=0, description="Minimum seconds between firings of this rule."
    )

    def params_dict(self) -> dict[str, Any]:
        """Merge the convenience field into the free-form params."""
        merged = dict(self.params)
        if self.target_price is not None:
            merged["target_price"] = str(self.target_price)
        return merged


class AlertUpdate(BaseModel):
    """Change an existing rule without recreating it.

    Both fields are optional and only the ones actually present in the request body are
    applied -- ``model_fields_set`` distinguishes "omitted" from "sent as null". So
    ``{"enabled": false}`` leaves the cooldown alone, and ``{"cooldown_seconds": null}``
    removes the gap without touching the enabled flag.
    """

    cooldown_seconds: int | None = Field(
        default=None, ge=0, description="Minimum seconds between firings. null removes the gap."
    )
    enabled: bool | None = Field(
        default=None, description="Turn the rule on or off. It keeps its history either way."
    )


class AlertResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    product_id: int
    rule_type: RuleType
    params: dict[str, Any]
    notify_provider: str | None
    enabled: bool
    cooldown_seconds: int | None
    last_fired_at: datetime | None
    created_at: datetime
    updated_at: datetime


class NotificationResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    product_id: int
    tracking_rule_id: int | None
    event_type: str
    status: str
    provider: str | None
    attempts: int
    error: str | None
    payload: dict[str, Any]
    created_at: datetime
    sent_at: datetime | None
