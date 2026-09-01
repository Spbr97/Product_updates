"""Running a check and then delivering what it produced.

Two transactions, deliberately:

1. **Check and record.** Fetch, write history, evaluate rules, insert notification rows as
   ``pending``. Commit.
2. **Deliver.** Hand the pending rows to providers in a *separate* transaction.

Splitting them is the point. Delivery talks to SMTP servers and webhook endpoints; holding
a PostgreSQL transaction open across that would pin a connection for as long as the slowest
provider takes, and a hung provider would hold it indefinitely.

The split is safe because the notification row is written and committed before anything is
sent: a crash between the two leaves a pending row that the next pass -- or the worker's
periodic retry -- picks up. Nothing is lost, and nothing is sent twice, because the unique
``dedupe_key`` was already claimed in step one.

Every caller goes through here: the CLI, the API, and the worker. Session handling for a
check lives in one place.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..core.config import Settings
from ..core.logging import get_logger
from ..db.models import CheckExecution
from ..db.session import session_scope
from ..domain.enums import Availability, CheckStatus, FetchMethod
from ..domain.models import CheckGuard
from ..notifications.base import NotificationProvider
from ..stores.registry import StoreRegistry, default_registry
from .notification_service import DeliveryReport, NotificationService
from .tracking import TrackingEngine

log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class CheckOutcome:
    """A check's result, detached from the session that produced it.

    Plain values rather than the ORM row: the transaction is closed by the time a caller
    reads this, and a detached instance would be a trap for anyone who touched a
    relationship.
    """

    execution_id: int
    product_id: int
    status: CheckStatus
    fetch_method: FetchMethod
    availability: Availability | None
    price: str | None
    currency: str | None
    duration_ms: int | None
    attempts: int
    error_type: str | None
    error_detail: str | None
    notifications_created: int
    notifications_sent: int = 0

    @property
    def failed(self) -> bool:
        return self.status is CheckStatus.FAILED

    @classmethod
    def from_execution(cls, execution: CheckExecution) -> CheckOutcome:
        return cls(
            execution_id=execution.id,
            product_id=execution.product_id,
            status=execution.status,
            fetch_method=execution.fetch_method,
            availability=execution.availability_result,
            price=str(execution.extracted_price)
            if execution.extracted_price is not None
            else None,
            currency=execution.extracted_currency,
            duration_ms=execution.duration_ms,
            attempts=execution.attempts,
            error_type=execution.error_type,
            error_detail=execution.error_detail,
            notifications_created=execution.notifications_created,
        )


def run_check(
    product_id: int,
    *,
    settings: Settings,
    registry: StoreRegistry | None = None,
    providers: list[NotificationProvider] | None = None,
    guard: CheckGuard | None = None,
    deliver: bool = True,
) -> CheckOutcome:
    """Check one product, then deliver any notifications it produced.

    ``deliver=False`` records without sending, which is what a bulk pass wants when
    delivery should happen once at the end rather than per product.
    """
    engine = TrackingEngine(
        registry or default_registry(), settings, providers=providers, guard=guard
    )

    with session_scope() as session:
        execution = engine.check_product(session, product_id)
        outcome = CheckOutcome.from_execution(execution)

    if not deliver or not outcome.notifications_created:
        return outcome

    report = deliver_pending(settings, providers=providers, product_id=product_id)
    return replace_sent(outcome, report.sent)


def deliver_pending(
    settings: Settings,
    *,
    providers: list[NotificationProvider] | None = None,
    product_id: int | None = None,
    limit: int = 50,
) -> DeliveryReport:
    """Send notifications recorded but not yet delivered. Its own transaction."""
    with session_scope() as session:
        service = NotificationService(session, settings, providers=providers)
        return service.deliver_pending(limit=limit, product_id=product_id)


def replace_sent(outcome: CheckOutcome, sent: int) -> CheckOutcome:
    """Return a copy with the delivery count filled in."""
    return CheckOutcome(
        execution_id=outcome.execution_id,
        product_id=outcome.product_id,
        status=outcome.status,
        fetch_method=outcome.fetch_method,
        availability=outcome.availability,
        price=outcome.price,
        currency=outcome.currency,
        duration_ms=outcome.duration_ms,
        attempts=outcome.attempts,
        error_type=outcome.error_type,
        error_detail=outcome.error_detail,
        notifications_created=outcome.notifications_created,
        notifications_sent=sent,
    )
