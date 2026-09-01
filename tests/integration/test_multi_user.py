"""Several users, one set of listings.

Two claims are being tested here, and they pull in opposite directions:

* **Listings are shared.** Two people watching the same URL must produce one product row,
  one scheduled job, and one fetch. Anything else means a retailer is hit twice for the
  same information, and the second user starts with an empty price chart.
* **Intent is private.** Subscriptions, groups and alert rules belong to whoever created
  them, and one user must not be able to see, change, or destroy another's.

The isolation tests are deliberately written as "B tries to touch A's thing and is told it
does not exist", not "is told it is forbidden". A 403 confirms the resource exists, which
lets a caller enumerate other people's data by watching which ids answer differently.
"""

from __future__ import annotations

from collections.abc import Iterator
from decimal import Decimal

import httpx
import pytest
import respx
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from tests.unit.test_adapters import load

from product_tracker.api.app import create_app
from product_tracker.core.config import get_settings
from product_tracker.db.models import PriceHistory, Product, Subscription, TrackingRule
from product_tracker.domain.enums import RuleType, TrackingStatus
from product_tracker.domain.errors import DuplicateError, NotFoundError
from product_tracker.repositories.users import SubscriptionRepository
from product_tracker.services import group_service, user_service
from product_tracker.services.alert_service import AlertService
from product_tracker.services.comparison import GroupNotFoundError, build_matrix
from product_tracker.services.product_service import ProductService
from product_tracker.stores.registry import default_registry

pytestmark = pytest.mark.db

URL = "https://shop.example.com/p/shared-listing"


@pytest.fixture(autouse=True)
def _respx_router() -> Iterator[None]:
    with respx.mock:
        yield


@pytest.fixture
def alice(db_session: Session) -> int:
    return int(user_service.create_user(db_session, email="alice@example.com").user.id)


@pytest.fixture
def bob(db_session: Session) -> int:
    return int(user_service.create_user(db_session, email="bob@example.com").user.id)


def service(session: Session, user_id: int) -> ProductService:
    return ProductService(session, default_registry(), get_settings(), user_id)


def track(session: Session, user_id: int, url: str = URL) -> Product:
    return service(session, user_id).add(url)


class TestListingsAreShared:
    def test_two_users_tracking_one_url_share_a_single_row(
        self, db_session: Session, alice: int, bob: int
    ) -> None:
        """One row, one job, one fetch -- however many people watch it."""
        first = track(db_session, alice)
        second = track(db_session, bob)

        assert first.id == second.id
        products = db_session.execute(select(func.count()).select_from(Product)).scalar_one()
        assert products == 1
        subscriptions = db_session.execute(
            select(func.count()).select_from(Subscription)
        ).scalar_one()
        assert subscriptions == 2

    def test_the_second_user_inherits_the_existing_history(
        self, db_session: Session, alice: int, bob: int
    ) -> None:
        """Joining a tracked listing should not mean starting from an empty chart."""
        product = track(db_session, alice)
        db_session.add(
            PriceHistory(product_id=product.id, price=Decimal("82900.00"), currency="INR")
        )
        db_session.flush()

        joined = track(db_session, bob)

        history = db_session.execute(
            select(func.count())
            .select_from(PriceHistory)
            .where(PriceHistory.product_id == joined.id)
        ).scalar_one()
        assert history == 1

    def test_re_adding_your_own_listing_is_still_a_duplicate(
        self, db_session: Session, alice: int
    ) -> None:
        track(db_session, alice)
        with pytest.raises(DuplicateError):
            track(db_session, alice)


class TestWatchlistsAreSeparate:
    def test_a_list_shows_only_your_own_subscriptions(
        self, db_session: Session, alice: int, bob: int
    ) -> None:
        track(db_session, alice, "https://shop.example.com/p/alice-only")
        track(db_session, bob, "https://shop.example.com/p/bob-only")

        alice_urls = {p.url for p in service(db_session, alice).list().items}
        bob_urls = {p.url for p in service(db_session, bob).list().items}

        assert alice_urls == {"https://shop.example.com/p/alice-only"}
        assert bob_urls == {"https://shop.example.com/p/bob-only"}

    def test_totals_are_scoped_too(self, db_session: Session, alice: int, bob: int) -> None:
        """A count that ignored the subscription would leak how much others track."""
        track(db_session, alice, "https://shop.example.com/p/a1")
        track(db_session, alice, "https://shop.example.com/p/a2")
        track(db_session, bob, "https://shop.example.com/p/b1")

        assert service(db_session, alice).list().total == 2
        assert service(db_session, bob).list().total == 1


class TestRemovingIsUnsubscribing:
    def test_removing_leaves_the_listing_for_everyone_else(
        self, db_session: Session, alice: int, bob: int
    ) -> None:
        """The destructive version of this bug deletes another user's months of history."""
        product = track(db_session, alice)
        track(db_session, bob)
        product_id = product.id
        db_session.add(
            PriceHistory(product_id=product_id, price=Decimal("82900.00"), currency="INR")
        )
        db_session.flush()

        service(db_session, alice).remove(product_id)

        assert db_session.get(Product, product_id) is not None
        assert service(db_session, bob).list().total == 1
        history = db_session.execute(
            select(func.count())
            .select_from(PriceHistory)
            .where(PriceHistory.product_id == product_id)
        ).scalar_one()
        assert history == 1

    def test_the_last_subscriber_leaving_deletes_it(
        self, db_session: Session, alice: int, bob: int
    ) -> None:
        product = track(db_session, alice)
        track(db_session, bob)
        product_id = product.id

        service(db_session, alice).remove(product_id)
        service(db_session, bob).remove(product_id)

        assert db_session.get(Product, product_id) is None


class TestPausingIsPerSubscriber:
    def test_one_user_pausing_does_not_silence_another(
        self, db_session: Session, alice: int, bob: int
    ) -> None:
        """Were pausing only a column on the listing, Bob would quietly stop getting
        updates with nothing in his own view to explain why."""
        product = track(db_session, alice)
        track(db_session, bob)

        AlertService(db_session, alice).set_tracking_status(product.id, TrackingStatus.PAUSED)

        db_session.refresh(product)
        assert product.tracking_status is TrackingStatus.ACTIVE

    def test_the_listing_stops_when_everyone_has_paused(
        self, db_session: Session, alice: int, bob: int
    ) -> None:
        product = track(db_session, alice)
        track(db_session, bob)

        AlertService(db_session, alice).set_tracking_status(product.id, TrackingStatus.PAUSED)
        AlertService(db_session, bob).set_tracking_status(product.id, TrackingStatus.PAUSED)

        db_session.refresh(product)
        assert product.tracking_status is TrackingStatus.PAUSED

    def test_resuming_reactivates_it(self, db_session: Session, alice: int, bob: int) -> None:
        product = track(db_session, alice)
        track(db_session, bob)
        AlertService(db_session, alice).set_tracking_status(product.id, TrackingStatus.PAUSED)
        AlertService(db_session, bob).set_tracking_status(product.id, TrackingStatus.PAUSED)

        AlertService(db_session, bob).set_tracking_status(product.id, TrackingStatus.ACTIVE)

        db_session.refresh(product)
        assert product.tracking_status is TrackingStatus.ACTIVE

    def test_pausing_something_you_do_not_watch_is_not_found(
        self, db_session: Session, alice: int, bob: int
    ) -> None:
        product = track(db_session, alice)
        with pytest.raises(NotFoundError):
            AlertService(db_session, bob).set_tracking_status(
                product.id, TrackingStatus.PAUSED
            )


class TestAlertsArePrivate:
    def test_two_users_may_hold_different_targets_on_one_listing(
        self, db_session: Session, alice: int, bob: int
    ) -> None:
        """Bob's rule is not a duplicate of Alice's just because it is the same type."""
        product = track(db_session, alice)
        track(db_session, bob)

        AlertService(db_session, alice).add(
            product.id, RuleType.PRICE_BELOW_TARGET, params={"target_price": "80000"}
        )
        AlertService(db_session, bob).add(
            product.id, RuleType.PRICE_BELOW_TARGET, params={"target_price": "70000"}
        )

        rules = db_session.execute(select(func.count()).select_from(TrackingRule)).scalar_one()
        assert rules == 2

    def test_a_list_shows_only_your_own_rules(
        self, db_session: Session, alice: int, bob: int
    ) -> None:
        product = track(db_session, alice)
        track(db_session, bob)
        AlertService(db_session, alice).add(product.id, RuleType.PRICE_DROPPED)

        assert AlertService(db_session, alice).list().total == 1
        assert AlertService(db_session, bob).list().total == 0

    def test_another_users_rule_is_not_found(
        self, db_session: Session, alice: int, bob: int
    ) -> None:
        product = track(db_session, alice)
        rule = AlertService(db_session, alice).add(product.id, RuleType.PRICE_DROPPED)

        with pytest.raises(NotFoundError):
            AlertService(db_session, bob).get(rule.id)

    def test_another_users_rule_cannot_be_deleted(
        self, db_session: Session, alice: int, bob: int
    ) -> None:
        product = track(db_session, alice)
        rule = AlertService(db_session, alice).add(product.id, RuleType.PRICE_DROPPED)

        with pytest.raises(NotFoundError):
            AlertService(db_session, bob).remove(rule.id)

        assert db_session.get(TrackingRule, rule.id) is not None

    def test_every_subscribers_rules_are_evaluated_on_one_check(
        self, db_session: Session, alice: int, bob: int
    ) -> None:
        """One fetch serves everyone, so it must evaluate everyone's rules.

        Scoping the engine's rule lookup to a user would mean only the first subscriber
        ever got alerted -- a silent failure for everybody else.
        """
        from product_tracker.repositories.rules import TrackingRuleRepository

        product = track(db_session, alice)
        track(db_session, bob)
        AlertService(db_session, alice).add(product.id, RuleType.PRICE_DROPPED)
        AlertService(db_session, bob).add(product.id, RuleType.PRICE_INCREASED)

        evaluated = TrackingRuleRepository(db_session).list_enabled(product.id)
        assert {rule.user_id for rule in evaluated} == {alice, bob}


class TestGroupsArePrivate:
    def test_two_users_may_use_the_same_slug(
        self, db_session: Session, alice: int, bob: int
    ) -> None:
        group_service.create_group(db_session, user_id=alice, slug="iphone-17", name="iPhone 17")
        group_service.create_group(db_session, user_id=bob, slug="iphone-17", name="iPhone 17")

        assert group_service.get_group(db_session, alice, "iphone-17").user_id == alice
        assert group_service.get_group(db_session, bob, "iphone-17").user_id == bob

    def test_a_list_shows_only_your_own_groups(
        self, db_session: Session, alice: int, bob: int
    ) -> None:
        from product_tracker.repositories.groups import GroupRepository

        group_service.create_group(db_session, user_id=alice, slug="alice-group", name="A")

        assert [g.slug for g in GroupRepository(db_session).list_all(alice)] == ["alice-group"]
        assert GroupRepository(db_session).list_all(bob) == []

    def test_another_users_group_is_not_found(
        self, db_session: Session, alice: int, bob: int
    ) -> None:
        group_service.create_group(db_session, user_id=alice, slug="secret", name="Secret")

        with pytest.raises(NotFoundError):
            group_service.get_group(db_session, bob, "secret")

    def test_another_users_group_cannot_be_deleted(
        self, db_session: Session, alice: int, bob: int
    ) -> None:
        group = group_service.create_group(
            db_session, user_id=alice, slug="secret", name="Secret"
        )

        with pytest.raises(NotFoundError):
            group_service.delete_group(db_session, bob, "secret")

        assert group_service.get_group(db_session, alice, group.slug) is not None

    def test_another_users_group_cannot_be_compared(
        self, db_session: Session, alice: int, bob: int
    ) -> None:
        group_service.create_group(db_session, user_id=alice, slug="secret", name="Secret")

        with pytest.raises(GroupNotFoundError):
            build_matrix(db_session, "secret", user_id=bob)

    def test_grouping_a_shared_listing_does_not_steal_it_from_another_user(
        self, db_session: Session, alice: int, bob: int
    ) -> None:
        """Regression, found by running two accounts against the live database.

        The listing-to-model link used to be a single ``products.variant_id`` column on a
        row several users share, so the second person to group a listing silently removed
        it from the first person's comparison. Alice's grid simply lost two of its five
        shops, with nothing anywhere to say why. The link now lives in its own table.
        """
        product = track(db_session, alice)
        track(db_session, bob)
        for user_id in (alice, bob):
            group_service.create_group(
                db_session, user_id=user_id, slug="iphone-17", name="iPhone 17"
            )

        group_service.attach_product(
            db_session, product.id, user_id=alice, group_slug="iphone-17", label="256GB / Black"
        )
        group_service.attach_product(
            db_session, product.id, user_id=bob, group_slug="iphone-17", label="256GB / Sage"
        )

        # Both groupings survive, and each user sees their own label.
        alice_rows = build_matrix(db_session, "iphone-17", user_id=alice).rows
        bob_rows = build_matrix(db_session, "iphone-17", user_id=bob).rows
        assert [row.label for row in alice_rows] == ["256GB / Black"]
        assert [row.label for row in bob_rows] == ["256GB / Sage"]
        assert any(cell.product_id == product.id for cell in alice_rows[0].cells.values())
        assert any(cell.product_id == product.id for cell in bob_rows[0].cells.values())

    def test_detaching_leaves_another_users_grouping_intact(
        self, db_session: Session, alice: int, bob: int
    ) -> None:
        product = track(db_session, alice)
        track(db_session, bob)
        for user_id in (alice, bob):
            group_service.create_group(
                db_session, user_id=user_id, slug="iphone-17", name="iPhone 17"
            )
            group_service.attach_product(
                db_session,
                product.id,
                user_id=user_id,
                group_slug="iphone-17",
                label="256GB / Black",
            )

        group_service.detach_product(db_session, product.id, alice)

        assert build_matrix(db_session, "iphone-17", user_id=alice).rows[0].cells == {}
        bob_row = build_matrix(db_session, "iphone-17", user_id=bob).rows[0]
        assert any(cell.product_id == product.id for cell in bob_row.cells.values())

    def test_a_listing_cannot_be_attached_to_someone_elses_group(
        self, db_session: Session, alice: int, bob: int
    ) -> None:
        group_service.create_group(db_session, user_id=alice, slug="secret", name="Secret")
        product = track(db_session, bob)

        with pytest.raises(NotFoundError):
            group_service.attach_product(
                db_session, product.id, user_id=bob, group_slug="secret"
            )


class TestApiKeys:
    """End to end through the API, which is where authentication actually happens."""

    @pytest.fixture
    def client(self, clean_db: None) -> Iterator[TestClient]:
        with TestClient(create_app(), raise_server_exceptions=False) as test_client:
            yield test_client

    @staticmethod
    def make_user(email: str) -> str:
        from product_tracker.db.session import session_scope

        with session_scope() as session:
            return user_service.create_user(session, email=email).api_key

    def test_a_key_identifies_its_owner(self, client: TestClient) -> None:
        alice_key = self.make_user("alice@example.com")
        bob_key = self.make_user("bob@example.com")

        client.post(
            "/api/v1/groups", json={"name": "Alice Group"}, headers={"X-API-Key": alice_key}
        )

        mine = client.get("/api/v1/groups", headers={"X-API-Key": alice_key}).json()
        theirs = client.get("/api/v1/groups", headers={"X-API-Key": bob_key}).json()
        assert [g["slug"] for g in mine] == ["alice-group"]
        assert theirs == []

    def test_another_users_group_is_a_404_not_a_403(self, client: TestClient) -> None:
        alice_key = self.make_user("alice@example.com")
        bob_key = self.make_user("bob@example.com")
        client.post(
            "/api/v1/groups", json={"name": "Secret"}, headers={"X-API-Key": alice_key}
        )

        response = client.get("/api/v1/groups/secret", headers={"X-API-Key": bob_key})

        # 403 would confirm it exists, which is itself a disclosure.
        assert response.status_code == 404

    def test_the_first_key_switches_authentication_on(self, client: TestClient) -> None:
        """Before any key exists the API is open; creating one locks it down."""
        assert client.get("/api/v1/groups").status_code == 200

        self.make_user("alice@example.com")

        assert client.get("/api/v1/groups").status_code == 401

    def test_an_unknown_key_is_rejected(self, client: TestClient) -> None:
        self.make_user("alice@example.com")

        response = client.get("/api/v1/groups", headers={"X-API-Key": "pt_not-a-real-key"})

        assert response.status_code == 401
        assert "WWW-Authenticate" in response.headers

    def test_rotating_invalidates_the_previous_key(self, client: TestClient) -> None:
        from product_tracker.db.session import session_scope

        old_key = self.make_user("alice@example.com")
        assert client.get("/api/v1/groups", headers={"X-API-Key": old_key}).status_code == 200

        with session_scope() as session:
            new_key = user_service.rotate_key(session, "alice@example.com").api_key

        assert client.get("/api/v1/groups", headers={"X-API-Key": old_key}).status_code == 401
        assert client.get("/api/v1/groups", headers={"X-API-Key": new_key}).status_code == 200

    def test_a_deactivated_account_cannot_authenticate(self, client: TestClient) -> None:
        from product_tracker.db.session import session_scope

        key = self.make_user("alice@example.com")
        with session_scope() as session:
            user_service.set_active(session, "alice@example.com", active=False)

        assert client.get("/api/v1/groups", headers={"X-API-Key": key}).status_code == 401

    def test_deactivating_the_last_key_holder_does_not_unlock_the_api(
        self, client: TestClient
    ) -> None:
        """Regression. Authentication is enabled by "does any key exist"; asking instead
        whether any *active* account holds one meant that disabling the only such account
        turned authentication off and opened the API to anonymous callers -- the exact
        opposite of what disabling an account is for.
        """
        from product_tracker.db.session import session_scope

        self.make_user("alice@example.com")
        with session_scope() as session:
            user_service.set_active(session, "alice@example.com", active=False)

        assert client.get("/api/v1/groups").status_code == 401

    def test_the_key_is_never_stored_in_the_clear(self, client: TestClient) -> None:
        from product_tracker.db.session import session_scope

        key = self.make_user("alice@example.com")
        with session_scope() as session:
            user = user_service.get_user(session, "alice@example.com")
            stored = user.api_key_hash

        assert stored is not None
        assert key not in stored
        assert len(stored) == 64  # SHA-256, hex.

    def test_two_users_adding_one_url_share_the_listing(self, client: TestClient) -> None:
        alice_key = self.make_user("alice@example.com")
        bob_key = self.make_user("bob@example.com")
        respx.get(URL).mock(return_value=httpx.Response(200, html=load("jsonld_in_stock.html")))

        first = client.post(
            "/api/v1/products", json={"url": URL}, headers={"X-API-Key": alice_key}
        )
        second = client.post(
            "/api/v1/products", json={"url": URL}, headers={"X-API-Key": bob_key}
        )

        assert first.status_code == 201
        # Bob joins the existing listing rather than being refused as a duplicate.
        assert second.status_code in {200, 201}
        assert first.json()["id"] == second.json()["id"]


class TestSubscriptionBookkeeping:
    def test_subscribing_twice_is_a_no_op(self, db_session: Session, alice: int) -> None:
        product = track(db_session, alice)

        user_service.subscribe(db_session, alice, product.id)
        user_service.subscribe(db_session, alice, product.id)

        assert SubscriptionRepository(db_session).subscriber_count(product.id) == 1

    def test_unsubscribing_reports_whether_anything_changed(
        self, db_session: Session, alice: int, bob: int
    ) -> None:
        product = track(db_session, alice)

        assert user_service.unsubscribe(db_session, alice, product.id) is True
        assert user_service.unsubscribe(db_session, bob, product.id) is False

    def test_subscribing_to_a_missing_listing_is_not_found(
        self, db_session: Session, alice: int
    ) -> None:
        with pytest.raises(NotFoundError):
            user_service.subscribe(db_session, alice, 99999)

    def test_deleting_a_user_leaves_the_listing_alone(
        self, db_session: Session, alice: int, bob: int
    ) -> None:
        product = track(db_session, alice)
        track(db_session, bob)
        product_id = product.id

        user_service.delete_user(db_session, alice)
        db_session.flush()

        assert db_session.get(Product, product_id) is not None
        assert SubscriptionRepository(db_session).subscriber_count(product_id) == 1


class TestOneUserCannotReadAnothersListings:
    """Product ids are sequential, which makes them walkable.

    Groups and alerts were scoped from the start; listings and their history were not, and
    an authenticated user could read every URL and price anyone else tracked by counting
    upwards from 1.
    """

    def test_a_listing_you_do_not_watch_is_not_found(
        self, db_session: Session, alice: int, bob: int
    ) -> None:
        product = track(db_session, alice)

        with pytest.raises(NotFoundError):
            service(db_session, bob).get(product.id)

    def test_the_owner_can_still_read_it(self, db_session: Session, alice: int) -> None:
        product = track(db_session, alice)
        assert service(db_session, alice).get(product.id).id == product.id

    def test_a_shared_listing_is_readable_by_both(
        self, db_session: Session, alice: int, bob: int
    ) -> None:
        """Scoping is by subscription, not by who added it first."""
        product = track(db_session, alice)
        track(db_session, bob)

        assert service(db_session, bob).get(product.id).id == product.id

    def test_removing_someone_elses_listing_is_not_found(
        self, db_session: Session, alice: int, bob: int
    ) -> None:
        product = track(db_session, alice)

        with pytest.raises(NotFoundError):
            service(db_session, bob).remove(product.id)
        assert db_session.get(Product, product.id) is not None

    def test_an_alert_cannot_be_set_on_a_listing_you_do_not_watch(
        self, db_session: Session, alice: int, bob: int
    ) -> None:
        """Otherwise an alert is a way to learn another user's prices: subscribe to
        nothing, rule on everything, and let the system report their movements."""
        product = track(db_session, alice)

        with pytest.raises(NotFoundError):
            AlertService(db_session, bob).add(product.id, RuleType.PRICE_DROPPED)

    def test_internal_callers_are_not_scoped(self, db_session: Session, alice: int) -> None:
        """A check runs on behalf of every subscriber at once, so the engine must still
        reach any listing. It builds the service without a user for exactly that reason."""
        product = track(db_session, alice)
        unscoped = ProductService(db_session, default_registry(), get_settings())

        assert unscoped.get(product.id).id == product.id
