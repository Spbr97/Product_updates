"""The tracking engine.

Checks one product and records what happened. This module imports no concrete store and
no notification provider -- only the ``StoreRegistry`` interface -- which is the property
that lets stores, channels, and rules be added without touching it.

Ordering is deliberate: the network fetch happens *outside* any transaction, and all
database writes are applied afterwards in one short transaction. Holding a Postgres
transaction open across a 25-second HTTP request would pin a connection and block
autovacuum for no benefit.

Phase 2 scope: fetch, update the product, write a ``check_executions`` row. Price and
availability history arrive in phase 3, rule evaluation and notifications in phase 4, and
retries and throttling in phase 5.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from ..core.config import Settings
from ..core.logging import (
    EVENT_CHECK_FINISHED,
    EVENT_CHECK_STARTED,
    bind_context,
    clear_context,
    get_logger,
)
from ..db.models import CheckExecution, Product
from ..domain.enums import CheckStatus, FetchMethod, FetchOutcome
from ..domain.errors import NotFoundError
from ..domain.models import FetchContext, FetchResult
from ..repositories.executions import truncate_error
from ..repositories.products import ProductRepository
from ..stores.registry import StoreRegistry
from ..utils.urls import host_of, validate_url

log = get_logger(__name__)


class TrackingEngine:
    """Runs a check for one product and records the outcome."""

    def __init__(self, registry: StoreRegistry, settings: Settings) -> None:
        self.registry = registry
        self.settings = settings

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
        """Apply the result to the product and write the execution row."""
        finished = datetime.now(UTC)
        status = _status_for(result)

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
            attempts=1,
            error_type=None if status is CheckStatus.SUCCESS else result.outcome.value,
            error_detail=None if status is CheckStatus.SUCCESS else truncate_error(result.message),
        )
        session.add(execution)

        product.last_checked_at = finished
        if result.succeeded:
            product.last_success_at = finished
            product.consecutive_failures = 0
            self._apply(product, result)
        else:
            product.consecutive_failures += 1

        session.flush()
        return execution

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
