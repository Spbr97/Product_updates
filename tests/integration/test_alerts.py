"""Alerts end to end: rules fire, notifications are recorded once, providers deliver.

The central guarantee under test is idempotency -- the same alert, however many times it
is observed or retried, reaches the user once.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import respx
from sqlalchemy.orm import Session
from tests.unit.test_adapters import load

from product_tracker.core.config import get_settings
from product_tracker.domain.enums import NotificationStatus, RuleType, TrackingStatus
from product_tracker.domain.errors import (
    DuplicateError,
    NotFoundError,
    ValidationError,
)
from product_tracker.domain.models import NotificationMessage, RuleMatch
from product_tracker.notifications.base import NotificationProvider
from product_tracker.repositories.notifications import (
    MAX_DELIVERY_ATTEMPTS,
    NotificationRepository,
)
from product_tracker.services.alert_service import AlertService
from product_tracker.services.notification_service import (
    NotificationService,
    build_dedupe_key,
)
from product_tracker.services.product_service import ProductService
from product_tracker.services.tracking import TrackingEngine
from product_tracker.stores.registry import StoreRegistry

pytestmark = pytest.mark.db

URL = "https://shop.example.com/p/alerts"
CHEAPER = load("jsonld_in_stock.html").replace("69999.00", "59999.00")


@pytest.fixture(autouse=True)
def _respx_router() -> Iterator[None]:
    """Activate respx for every test in this module.

    Not ``@respx.mock`` on the class: in respx 0.23 that decorator returns a *function*,
    so pytest silently stops collecting the class and the tests never run.
    """
    with respx.mock:
        yield


class RecordingProvider(NotificationProvider):
    """Captures deliveries instead of sending them."""

    slug = "console"
    display_name = "Recording"

    def __init__(self, *, fail: bool = False) -> None:
        self.sent: list[NotificationMessage] = []
        self.fail = fail

    def is_configured(self) -> bool:
        return True

    def send(self, message: NotificationMessage) -> None:
        if self.fail:
            from product_tracker.domain.errors import NotificationDeliveryError

            raise NotificationDeliveryError(self.slug, "provider is down")
        self.sent.append(message)


@pytest.fixture
def provider() -> RecordingProvider:
    return RecordingProvider()


@pytest.fixture
def service(db_session: Session) -> ProductService:
    return ProductService(db_session, StoreRegistry(), get_settings())


@pytest.fixture
def alerts(db_session: Session) -> AlertService:
    return AlertService(db_session)


@pytest.fixture
def engine(db_env: None, provider: RecordingProvider) -> TrackingEngine:
    return TrackingEngine(StoreRegistry(), get_settings(), providers=[provider])


@pytest.fixture
def product_id(service: ProductService) -> int:
    return service.add(URL).id


def stub(html: str) -> None:
    respx.get(URL).mock(return_value=httpx.Response(200, html=html))


class TestAlertService:
    def test_adds_a_rule(self, alerts: AlertService, product_id: int) -> None:
        rule = alerts.add(product_id, RuleType.PRICE_DROPPED)

        assert rule.id is not None
        assert rule.enabled is True

    def test_validates_params_at_creation(
        self, alerts: AlertService, product_id: int
    ) -> None:
        """An unusable rule must be refused, not saved to silently never fire."""
        with pytest.raises(ValidationError, match="requires a target_price"):
            alerts.add(product_id, RuleType.PRICE_BELOW_TARGET)

    def test_rejects_an_unknown_provider(
        self, alerts: AlertService, product_id: int
    ) -> None:
        with pytest.raises(ValidationError, match="unknown notification provider"):
            alerts.add(product_id, RuleType.PRICE_DROPPED, notify_provider="carrier-pigeon")

    def test_rejects_a_duplicate_rule_type(
        self, alerts: AlertService, product_id: int
    ) -> None:
        alerts.add(product_id, RuleType.PRICE_DROPPED)

        with pytest.raises(DuplicateError):
            alerts.add(product_id, RuleType.PRICE_DROPPED)

    def test_allows_different_rule_types(
        self, alerts: AlertService, product_id: int
    ) -> None:
        alerts.add(product_id, RuleType.PRICE_DROPPED)
        alerts.add(product_id, RuleType.BECAME_AVAILABLE)

        assert alerts.list(product_id=product_id).total == 2

    def test_missing_product_raises(self, alerts: AlertService) -> None:
        with pytest.raises(NotFoundError):
            alerts.add(999_999, RuleType.PRICE_DROPPED)

    def test_remove(self, alerts: AlertService, product_id: int) -> None:
        rule_id = alerts.add(product_id, RuleType.PRICE_DROPPED).id

        alerts.remove(rule_id)

        with pytest.raises(NotFoundError):
            alerts.get(rule_id)

    def test_deleting_a_product_cascades_to_its_rules(
        self, alerts: AlertService, service: ProductService, product_id: int
    ) -> None:
        rule_id = alerts.add(product_id, RuleType.PRICE_DROPPED).id

        service.remove(product_id)

        with pytest.raises(NotFoundError):
            alerts.get(rule_id)


class TestPauseResume:
    def test_pause_and_resume(self, alerts: AlertService, product_id: int) -> None:
        paused = alerts.set_tracking_status(product_id, TrackingStatus.PAUSED)
        assert paused.tracking_status is TrackingStatus.PAUSED

        resumed = alerts.set_tracking_status(product_id, TrackingStatus.ACTIVE)
        assert resumed.tracking_status is TrackingStatus.ACTIVE

    def test_pausing_keeps_the_product(
        self, alerts: AlertService, service: ProductService, product_id: int
    ) -> None:
        alerts.set_tracking_status(product_id, TrackingStatus.PAUSED)
        assert service.get(product_id) is not None

    def test_missing_product_raises(self, alerts: AlertService) -> None:
        with pytest.raises(NotFoundError):
            alerts.set_tracking_status(999_999, TrackingStatus.PAUSED)


class TestRulesFireOnChecks:
    def test_price_drop_alerts(
        self,
        service: ProductService,
        alerts: AlertService,
        engine: TrackingEngine,
        provider: RecordingProvider,
        db_session: Session,
    ) -> None:
        stub(load("jsonld_in_stock.html"))
        product = service.add(URL)
        alerts.add(product.id, RuleType.PRICE_DROPPED)
        engine.check_product(db_session, product.id)  # baseline

        stub(CHEAPER)
        execution = engine.check_product(db_session, product.id)

        assert execution.rules_evaluated == 1
        assert execution.rules_matched == 1
        assert execution.notifications_created == 1
        assert len(provider.sent) == 1
        assert "Price drop" in provider.sent[0].title

    def test_first_check_does_not_alert(
        self,
        service: ProductService,
        alerts: AlertService,
        engine: TrackingEngine,
        provider: RecordingProvider,
        db_session: Session,
    ) -> None:
        """A baseline is not a change; alerting on it would fire for every new product."""
        stub(load("jsonld_in_stock.html"))
        product = service.add(URL)
        alerts.add(product.id, RuleType.PRICE_DROPPED)

        execution = engine.check_product(db_session, product.id)

        assert execution.rules_matched == 0
        assert provider.sent == []

    def test_target_price_alerts_without_a_prior_observation(
        self,
        service: ProductService,
        alerts: AlertService,
        engine: TrackingEngine,
        provider: RecordingProvider,
        db_session: Session,
    ) -> None:
        stub(load("jsonld_in_stock.html"))
        product = service.add(URL)
        alerts.add(
            product.id, RuleType.PRICE_BELOW_TARGET, params={"target_price": "70000"}
        )

        engine.check_product(db_session, product.id)

        assert len(provider.sent) == 1
        assert "Target price reached" in provider.sent[0].title

    def test_back_in_stock_alerts(
        self,
        service: ProductService,
        alerts: AlertService,
        engine: TrackingEngine,
        provider: RecordingProvider,
        db_session: Session,
    ) -> None:
        stub(load("jsonld_out_of_stock.html"))
        product = service.add(URL)
        alerts.add(product.id, RuleType.BECAME_AVAILABLE)
        engine.check_product(db_session, product.id)

        stub(load("jsonld_in_stock.html"))
        engine.check_product(db_session, product.id)

        assert len(provider.sent) == 1
        assert "Back in stock" in provider.sent[0].title

    def test_a_failed_check_fires_nothing(
        self,
        service: ProductService,
        alerts: AlertService,
        engine: TrackingEngine,
        provider: RecordingProvider,
        db_session: Session,
    ) -> None:
        """No observation, no alert -- a block must not look like a stock change."""
        stub(load("jsonld_in_stock.html"))
        product = service.add(URL)
        alerts.add(product.id, RuleType.BECAME_UNAVAILABLE)
        engine.check_product(db_session, product.id)

        respx.get(URL).mock(return_value=httpx.Response(403))
        engine.check_product(db_session, product.id)

        assert provider.sent == []

    def test_disabled_rules_do_not_fire(
        self,
        service: ProductService,
        alerts: AlertService,
        engine: TrackingEngine,
        provider: RecordingProvider,
        db_session: Session,
    ) -> None:
        stub(load("jsonld_in_stock.html"))
        product = service.add(URL)
        rule = alerts.add(product.id, RuleType.PRICE_DROPPED)
        engine.check_product(db_session, product.id)
        alerts.set_enabled(rule.id, False)

        stub(CHEAPER)
        engine.check_product(db_session, product.id)

        assert provider.sent == []

    def test_products_without_rules_cost_nothing(
        self,
        service: ProductService,
        engine: TrackingEngine,
        provider: RecordingProvider,
        db_session: Session,
    ) -> None:
        stub(load("jsonld_in_stock.html"))
        product = service.add(URL)

        execution = engine.check_product(db_session, product.id)

        assert execution.rules_evaluated == 0
        assert provider.sent == []


class TestDeduplication:
    """The same alert must reach the user once, however often it is observed."""

    def _drop(
        self,
        service: ProductService,
        alerts: AlertService,
        engine: TrackingEngine,
        db_session: Session,
    ) -> int:
        stub(load("jsonld_in_stock.html"))
        product = service.add(URL)
        alerts.add(product.id, RuleType.PRICE_BELOW_TARGET, params={"target_price": "70000"})
        engine.check_product(db_session, product.id)
        return int(product.id)

    def test_repeated_observation_alerts_once(
        self,
        service: ProductService,
        alerts: AlertService,
        engine: TrackingEngine,
        provider: RecordingProvider,
        db_session: Session,
    ) -> None:
        """The target rule fires on state, so every check matches -- but one alert."""
        product_id = self._drop(service, alerts, engine, db_session)

        for _ in range(4):
            engine.check_product(db_session, product_id)

        assert len(provider.sent) == 1
        assert NotificationRepository(db_session).list_for_product(product_id).__len__() == 1

    def test_a_further_drop_alerts_again(
        self,
        service: ProductService,
        alerts: AlertService,
        engine: TrackingEngine,
        provider: RecordingProvider,
        db_session: Session,
    ) -> None:
        """Dedup must not swallow genuinely new news."""
        product_id = self._drop(service, alerts, engine, db_session)

        stub(CHEAPER)
        engine.check_product(db_session, product_id)

        assert len(provider.sent) == 2

    def test_the_unique_constraint_is_what_enforces_it(
        self,
        service: ProductService,
        alerts: AlertService,
        engine: TrackingEngine,
        db_session: Session,
    ) -> None:
        """Two racing writers cannot both insert; the database decides."""
        product_id = self._drop(service, alerts, engine, db_session)
        repo = NotificationRepository(db_session)
        existing = repo.list_for_product(product_id)[0]

        duplicate = repo.create_if_new(
            product_id=product_id,
            tracking_rule_id=existing.tracking_rule_id,
            event_type=existing.event_type,
            dedupe_key=existing.dedupe_key,
            payload={},
        )

        assert duplicate is None

    def test_cooldown_suppresses_a_genuine_second_alert(
        self,
        service: ProductService,
        alerts: AlertService,
        engine: TrackingEngine,
        provider: RecordingProvider,
        db_session: Session,
    ) -> None:
        stub(load("jsonld_in_stock.html"))
        product = service.add(URL)
        alerts.add(
            product.id,
            RuleType.PRICE_BELOW_TARGET,
            params={"target_price": "70000"},
            cooldown_seconds=3600,
        )
        engine.check_product(db_session, product.id)
        assert len(provider.sent) == 1

        stub(CHEAPER)
        engine.check_product(db_session, product.id)

        assert len(provider.sent) == 1  # still within the cooldown

    def test_cooldown_only_starts_when_something_was_actually_sent(
        self,
        service: ProductService,
        alerts: AlertService,
        engine: TrackingEngine,
        db_session: Session,
    ) -> None:
        """A deduplicated match has not alerted anyone, so it must not start a cooldown."""
        product_id = self._drop(service, alerts, engine, db_session)
        rule = alerts.list(product_id=product_id).items[0]
        first_fired = rule.last_fired_at

        engine.check_product(db_session, product_id)  # deduplicated

        assert rule.last_fired_at == first_fired


class TestDelivery:
    def test_failure_is_recorded_and_does_not_break_the_check(
        self,
        service: ProductService,
        alerts: AlertService,
        db_session: Session,
    ) -> None:
        broken = RecordingProvider(fail=True)
        engine = TrackingEngine(StoreRegistry(), get_settings(), providers=[broken])
        stub(load("jsonld_in_stock.html"))
        product = service.add(URL)
        alerts.add(product.id, RuleType.PRICE_BELOW_TARGET, params={"target_price": "70000"})

        execution = engine.check_product(db_session, product.id)

        assert execution.status.value == "success"  # the check itself succeeded
        notification = NotificationRepository(db_session).list_for_product(product.id)[0]
        assert notification.status is NotificationStatus.FAILED
        assert "provider is down" in (notification.error or "")

    def test_a_failed_notification_is_retried_later(
        self,
        service: ProductService,
        alerts: AlertService,
        db_session: Session,
    ) -> None:
        broken = RecordingProvider(fail=True)
        engine = TrackingEngine(StoreRegistry(), get_settings(), providers=[broken])
        stub(load("jsonld_in_stock.html"))
        product = service.add(URL)
        alerts.add(product.id, RuleType.PRICE_BELOW_TARGET, params={"target_price": "70000"})
        engine.check_product(db_session, product.id)

        working = RecordingProvider()
        report = NotificationService(
            db_session, get_settings(), providers=[working]
        ).retry_pending()

        assert report.sent == 1
        assert len(working.sent) == 1

    def test_retries_are_bounded(
        self,
        service: ProductService,
        alerts: AlertService,
        db_session: Session,
    ) -> None:
        """A permanently broken provider must not be retried on every check forever."""
        broken = RecordingProvider(fail=True)
        engine = TrackingEngine(StoreRegistry(), get_settings(), providers=[broken])
        stub(load("jsonld_in_stock.html"))
        product = service.add(URL)
        alerts.add(product.id, RuleType.PRICE_BELOW_TARGET, params={"target_price": "70000"})
        engine.check_product(db_session, product.id)

        notifier = NotificationService(db_session, get_settings(), providers=[broken])
        for _ in range(MAX_DELIVERY_ATTEMPTS + 2):
            notifier.retry_pending()

        notification = NotificationRepository(db_session).list_for_product(product.id)[0]
        assert notification.attempts == MAX_DELIVERY_ATTEMPTS

    def test_no_configured_provider_suppresses_rather_than_looping(
        self,
        service: ProductService,
        alerts: AlertService,
        db_session: Session,
    ) -> None:
        engine = TrackingEngine(StoreRegistry(), get_settings(), providers=[])
        stub(load("jsonld_in_stock.html"))
        product = service.add(URL)
        alerts.add(product.id, RuleType.PRICE_BELOW_TARGET, params={"target_price": "70000"})

        engine.check_product(db_session, product.id)

        notification = NotificationRepository(db_session).list_for_product(product.id)[0]
        assert notification.status is NotificationStatus.SUPPRESSED

    def test_a_rule_provider_preference_is_honoured(
        self,
        service: ProductService,
        alerts: AlertService,
        db_session: Session,
    ) -> None:
        console = RecordingProvider()
        webhook = RecordingProvider()
        webhook.slug = "webhook"  # type: ignore[misc]
        engine = TrackingEngine(
            StoreRegistry(), get_settings(), providers=[console, webhook]
        )
        stub(load("jsonld_in_stock.html"))
        product = service.add(URL)
        alerts.add(
            product.id,
            RuleType.PRICE_BELOW_TARGET,
            params={"target_price": "70000"},
            notify_provider="webhook",
        )

        engine.check_product(db_session, product.id)

        assert len(webhook.sent) == 1
        assert console.sent == []

    def test_first_provider_that_succeeds_wins(
        self,
        service: ProductService,
        alerts: AlertService,
        db_session: Session,
    ) -> None:
        """A provider list means "reach me", not "tell me four times"."""
        broken = RecordingProvider(fail=True)
        working = RecordingProvider()
        working.slug = "webhook"  # type: ignore[misc]
        engine = TrackingEngine(StoreRegistry(), get_settings(), providers=[broken, working])
        stub(load("jsonld_in_stock.html"))
        product = service.add(URL)
        alerts.add(product.id, RuleType.PRICE_BELOW_TARGET, params={"target_price": "70000"})

        engine.check_product(db_session, product.id)

        assert len(working.sent) == 1
        notification = NotificationRepository(db_session).list_for_product(product.id)[0]
        assert notification.status is NotificationStatus.SENT
        assert notification.provider == "webhook"


class TestDedupeKey:
    def test_same_alert_on_a_later_day_is_a_different_key(self) -> None:
        """A price that drops every Monday is news every Monday."""
        match = RuleMatch(
            rule_id=1,
            rule_type=RuleType.PRICE_DROPPED,
            product_id=1,
            title="t",
            body="b",
            context={"previous_price": "100", "current_price": "90"},
        )
        monday = datetime(2026, 6, 1, 9, 0, tzinfo=UTC)

        assert build_dedupe_key(match, when=monday) != build_dedupe_key(
            match, when=monday + timedelta(days=7)
        )

    def test_same_alert_same_day_is_the_same_key(self) -> None:
        match = RuleMatch(
            rule_id=1,
            rule_type=RuleType.PRICE_DROPPED,
            product_id=1,
            title="t",
            body="b",
            context={"previous_price": "100", "current_price": "90"},
        )
        morning = datetime(2026, 6, 1, 9, 0, tzinfo=UTC)

        assert build_dedupe_key(match, when=morning) == build_dedupe_key(
            match, when=morning + timedelta(hours=8)
        )
