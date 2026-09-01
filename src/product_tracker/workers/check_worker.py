"""What a scheduled job actually runs.

Two rules govern this module:

1. **It must never raise.** An exception escaping here reaches APScheduler's executor,
   which logs it and carries on -- but the failure would be invisible in our own data. Every
   path records something instead.
2. **It owns its own session.** Jobs run on a thread pool, and a SQLAlchemy session is not
   thread-safe, so a session cannot be shared with the scheduler or with another job.

The per-store guard is process-wide and injected by the runner, so all jobs pace against
the same state. Without that, twenty concurrent Flipkart checks would each think they were
the only one.
"""

from __future__ import annotations

from ..core.config import get_settings
from ..core.logging import clear_context, get_logger
from ..db.session import session_scope
from ..domain.errors import NotFoundError
from ..domain.models import CheckGuard
from ..services.tracking import TrackingEngine
from ..stores.registry import default_registry

log = get_logger(__name__)

#: Set once by the runner. Jobs are plain module-level functions (APScheduler pickles a
#: reference to them), so the shared guard has to reach them through module state.
_GUARD: CheckGuard | None = None


def set_guard(guard: CheckGuard | None) -> None:
    global _GUARD
    _GUARD = guard


def build_engine() -> TrackingEngine:
    return TrackingEngine(default_registry(), get_settings(), guard=_GUARD)


def run_check(product_id: int) -> None:
    """Check one product. Swallows everything; the outcome lives in the database."""
    try:
        engine = build_engine()
        with session_scope() as session:
            engine.check_product(session, product_id)
    except NotFoundError:
        # The product was deleted between reconcile and this run. Not an error; the next
        # reconcile pass removes the job.
        log.info("job.product_gone", product_id=product_id)
    except Exception as exc:
        # A database outage, a bug -- anything. One product's problem must not stop the
        # scheduler checking every other product.
        log.error("job.failed", product_id=product_id, exc_info=exc)
    finally:
        clear_context()


def retry_notifications(limit: int = 50) -> None:
    """Re-attempt notifications an earlier pass could not deliver."""
    from ..services.notification_service import NotificationService

    try:
        with session_scope() as session:
            report = NotificationService(session, get_settings()).retry_pending(limit=limit)
        if report.sent or report.failed:
            log.info("notifications.retried", sent=report.sent, failed=report.failed)
    except Exception as exc:
        log.error("notifications.retry_failed", exc_info=exc)
