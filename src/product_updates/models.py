from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal


@dataclass(frozen=True)
class Offer:
    retailer: str
    title: str
    url: str
    price: Decimal | None
    available: bool
    delivery_charge: Decimal | None = None
    observed_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def key(self) -> str:
        return f"{self.retailer}|{self.url}"


@dataclass(frozen=True)
class Change:
    kind: str
    offer: Offer
    previous_price: Decimal | None = None
    previous_available: bool | None = None
