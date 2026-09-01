"""The tracking engine.

Checks one product and records what happened. This module imports no concrete store and
no notification provider -- only the ``StoreRegistry`` interface -- which is the property
that lets stores, channels, and rules be added without touching it.

Ordering is deliberate: the network fetch happens *outside* any transaction, and all
database writes are applied afterwards in one short transaction. Holding a Postgres
transaction open across a 25-second HTTP request would pin a connection and block
autovacuum for no benefit.

History is appended in the same transaction as the execution row, and every history row
carries the ``check_execution_id`` that produced it, so any recorded price can be traced
back to the fetch that saw it.

After history is recorded, the product's rules are evaluated and any matches become
notifications. None of that can fail a check: the observation is already stored, and a
misbehaving rule or an unreachable provider is logged and recorded rather than raised.

Retries and throttling arrive in phase 5.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from ..core.config import Settings
from ..core.logging import (
    EVENT_CHANGE_DETECTED,
    EVENT_CHECK_FINISHED,
    EVENT_CHECK_STARTED,
    bind_context,
    clear_context,
    get_logger,
)
from ..db.models import CheckExecution, Product
from ..domain.enums import Availability, CheckStatus, FetchMethod, FetchOutcome
from ..domain.errors import NotFoundError
from ..domain.models import FetchContext, FetchResult, ProductSnapshot, RuleContext
from ..notifications.base import NotificationProvider
from ..repositories.availability_history import AvailabilityHistoryRepository
from ..repositories.executions import truncate_error
from ..repositories.price_history import PriceHistoryRepository
from ..repositories.products import ProductRepository
from ..repositories.rules import TrackingRuleRepository
from ..stores.registry import StoreRegistry
from ..utils.urls import host_of, validate_url
from .change_detection import (
    AvailabilityOutcome,
    PriceOutcome,
    detect_availability_change,
    detect_price_change,
)
from .notification_service import NotificationService
from .rules_engine import evaluate_all

log = get_logger(__name__)


class TrackingEngine:
    """Runs a check for one product and records the outcome."""

    def __init__(
        self,
        registry: StoreRegistry,
        settings: Settings,
        providers: list[NotificationProvider] | None = None,
    ) -> None:
        self.registry = registry
        self.settings = settings
        # Injected in tests; resolved from settings on first use otherwise.
        self.providers = providers

    def fetch_context(self) -> FetchContext:
        return FetchContext(
            timeout_seconds=self.settings.http_timeout_seconds,
            allow_browser=self.settings.playwright_enabled,
            user_agent=self.settings.http_user_agent,
            accept_language=self.settings.http_accept_language,
            max_bytes=self.settings.http_max_response_bytes,
            verify_public_host=self.settings.block_private_addresses,
        )

    def check_product(self, session: Session, product_id: int) -> CheckExecution:
        """Check one product.

        Never raises for a store problem -- a failed fetch produces a ``failed``
        execution row explaining why. Only database errors propagate.
        """
        products = ProductRepository(session)
        product = products.get(product_id)
        if product is None:
            raise NotFoundError("Product", product_id)

        store_id = product.store_id
        store_slug = product.store.slug if product.store else None
        url = product.url

        bind_context(product_id=product_id, store=store_slug, url_host=host_of(url))
        log.info(EVENT_CHECK_STARTED, url_host=host_of(url))

        started = datetime.now(UTC)
        began = time.monotonic()

        # Outside any transaction: this is the slow part.
        result = self._fetch(url)

        duration_ms = int((time.monotonic() - began) * 1000)
        execution = self._record(
            session=session,
            product=product,
            result=result,
            store_id=store_id,
            started_at=started,
            duration_ms=duration_ms,
        )

        log.info(
            EVENT_CHECK_FINISHED,
            status=execution.status.value,
            outcome=result.outcome.value,
            availability=result.availability.value,
            price=str(result.price) if result.price is not None else None,
            duration_ms=duration_ms,
            error_type=execution.error_type,
        )
        clear_context()
        return execution

    def _fetch(self, url: str) -> FetchResult:
        """Resolve an adapter and fetch, converting any surprise into a FetchResult.

        The URL is re-validated here even though it was validated when added: the SSRF
        policy may have changed since, and a DNS record may now point somewhere private.
        """
        try:
            validate_url(
                url,
                allowed_schemes=self.settings.url_schemes,
                block_private=self.settings.block_private_addresses,
                max_length=self.settings.max_url_length,
            )
            adapter = self.registry.resolve(url)
            return adapter.fetch_product(url, self.fetch_context())
        except Exception as exc:
            # An adapter that raises is a bug, but one product's bug must not stop the
            # scheduler checking every other product.
            log.warning("check.adapter_error", error_type=type(exc).__name__, exc_info=exc)
            return FetchResult.failure(
                FetchOutcome.ERROR, f"{type(exc).__name__}: {exc}", fetch_method=FetchMethod.NONE
            )

    def _record(
        self,
        *,
        session: Session,
        product: Product,
        result: FetchResult,
        store_id: int,
        started_at: datetime,
        duration_ms: int,
    ) -> CheckExecution:
        """Apply the result to the product, append history, and write the execution row."""
        finished = datetime.now(UTC)
        status = _status_for(result)

        prices = PriceHistoryRepository(session)
        availabilities = AvailabilityHistoryRepository(session)

        last_price = prices.latest(product.id)
        last_availability = availabilities.latest(product.id)

        price_outcome = detect_price_change(
            result,
            last_price.price if last_price else None,
            last_price.currency if last_price else None,
        )
        availability_outcome = detect_availability_change(
            result, last_availability.availability if last_availability else None
        )

        execution = CheckExecution(
            product_id=product.id,
            store_id=store_id,
            started_at=started_at,
            finished_at=finished,
            duration_ms=duration_ms,
            status=status,
            fetch_method=result.fetch_method,
            http_status=result.http_status,
            extracted_price=result.price,
            extracted_currency=result.currency,
            availability_result=result.availability,
            price_changed=price_outcome.changed,
            availability_changed=availability_outcome.changed,
            attempts=1,
            error_type=None if status is CheckStatus.SUCCESS else result.outcome.value,
            error_detail=None if status is CheckStatus.SUCCESS else truncate_error(result.message),
        )
        session.add(execution)
        # Flush now so history rows can carry this execution's id as their provenance.
        session.flush()

        if price_outcome.should_record and result.price is not None:
            prices.record(
                product_id=product.id,
                price=result.price,
                currency=result.currency or product.currency or "",
                observed_at=finished,
                check_execution_id=execution.id,
            )
        if availability_outcome.should_record:
            availabilities.record(
                product_id=product.id,
                availability=result.availability,
                observed_at=finished,
                check_execution_id=execution.id,
            )

        if price_outcome.changed or availability_outcome.changed:
            log.info(
                EVENT_CHANGE_DETECTED,
                price_changed=price_outcome.changed,
                availability_changed=availability_outcome.changed,
                previous_price=str(price_outcome.previous)
                if price_outcome.previous is not None
                else None,
                price=str(price_outcome.current) if price_outcome.current is not None else None,
                previous_availability=availability_outcome.previous.value
                if availability_outcome.previous
                else None,
                availability=availability_outcome.current.value,
            )

        product.last_checked_at = finished
        if result.succeeded:
            product.last_success_at = finished
            product.consecutive_failures = 0
            self._apply(product, result)
        else:
            product.consecutive_failures += 1

        self._evaluate_rules(
            session=session,
            product=product,
            execution=execution,
            price_outcome=price_outcome,
            availability_outcome=availability_outcome,
            now=finished,
        )

        session.flush()
        return execution

    def _evaluate_rules(
        self,
        *,
        session: Session,
        product: Product,
        execution: CheckExecution,
        price_outcome: PriceOutcome,
        availability_outcome: AvailabilityOutcome,
        now: datetime,
    ) -> None:
        """Evaluate this product's rules and dispatch any resulting notifications.

        Nothing here can fail a check. A rule that raises, or a provider that will not
        deliver, is logged and recorded; the observation is already safely stored.
        """
        rules_repo = TrackingRuleRepository(session)
        rules = rules_repo.list_enabled(product.id)
        execution.rules_evaluated = len(rules)
        if not rules:
            return

        context = RuleContext(
            product=_snapshot(product),
            previous_price=price_outcome.previous,
            current_price=price_outcome.current if price_outcome.current is not None
            else product.current_price,
            previous_availability=availability_outcome.previous or Availability.UNKNOWN,
            current_availability=availability_outcome.current,
            stats=None,
            observed_at=now,
        )

        try:
            matches = evaluate_all(rules, context, now=now)
        except Exception as exc:
            log.warning("rule.evaluation_failed", product_id=product.id, exc_info=exc)
            return

        execution.rules_matched = len(matches)
        if not matches:
            return

        notifier = self._notifier(session)
        report = notifier.dispatch(matches, when=now)
        execution.notifications_created = report.created

        # Only rules that produced a *new* notification start their cooldown; one that
        # was deduplicated has not actually alerted anyone.
        if report.created:
            fired = {match.rule_id for match in matches}
            for rule in rules:
                if rule.id in fired:
                    rules_repo.mark_fired(rule, now)

    def _notifier(self, session: Session) -> NotificationService:
        return NotificationService(session, self.settings, providers=self.providers)

    def _apply(self, product: Product, result: FetchResult) -> None:
        """Copy a successful reading onto the product row.

        Only fields the adapter actually found are overwritten: a check that returns a
        price but no image must not erase the image we already had.
        """
        if result.price is not None:
            product.current_price = result.price
        if result.currency:
            product.currency = result.currency
        if result.name:
            product.name = result.name
        if result.product_identifier:
            product.product_identifier = result.product_identifier
        if result.image_url:
            product.image_url = result.image_url

        # Availability is assigned even when UNKNOWN: "we no longer know" is itself a
        # change worth reflecting, unlike a missing name.
        product.availability = result.availability

        if result.raw_metadata:
            product.extra_metadata = {**product.extra_metadata, **result.raw_metadata}


def _status_for(result: FetchResult) -> CheckStatus:
    """Map a fetch outcome onto the recorded check status.

    ``OUT_OF_STOCK`` and ``UNAVAILABLE`` are successes: the check did its job and learned
    the truth. ``PRICE_NOT_FOUND`` is ``partial`` -- the page was readable but did not
    give us the number we came for.
    """
    if result.outcome is FetchOutcome.OK:
        return CheckStatus.SUCCESS
    if result.outcome in (FetchOutcome.OUT_OF_STOCK, FetchOutcome.UNAVAILABLE):
        return CheckStatus.SUCCESS
    if result.outcome is FetchOutcome.PRICE_NOT_FOUND:
        return CheckStatus.PARTIAL
    return CheckStatus.FAILED


def _snapshot(product: Product) -> ProductSnapshot:
    """Detach the fields rules may read, so evaluators cannot touch the session."""
    return ProductSnapshot(
        id=product.id,
        url=product.url,
        store_slug=product.store.slug if product.store else "",
        name=product.name,
        current_price=product.current_price,
        currency=product.currency,
        availability=product.availability,
        tracking_status=product.tracking_status,
        last_checked_at=product.last_checked_at,
    )
