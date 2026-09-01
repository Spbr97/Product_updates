"""Groups, variants, and the comparison grid, against a real database.

Two properties here are worth more than the rest put together:

* deleting a group must not delete price history -- grouping is an overlay, and a person
  reorganising their comparison must never be able to destroy months of observations;
* the grid must load in a fixed number of queries, because the natural implementation
  (walk the variants, then walk each one's listings) looks perfect against the four rows
  in a test and collapses against a real catalogue.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import event, func, select
from sqlalchemy.orm import Session

from product_tracker.db.models import (
    CheckExecution,
    PriceHistory,
    Product,
    ProductGroup,
    ProductVariant,
    Store,
    VariantListing,
)
from product_tracker.domain.enums import (
    Availability,
    CellStatus,
    CheckStatus,
    FetchMethod,
    FetchOutcome,
)
from product_tracker.domain.errors import DuplicateError, NotFoundError, ValidationError
from product_tracker.repositories.groups import GroupRepository
from product_tracker.services import group_service
from product_tracker.services.comparison import GroupNotFoundError, build_matrix

pytestmark = pytest.mark.db

NOW = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)


@contextmanager
def counted_queries(session: Session) -> Iterator[list[str]]:
    """Record every SQL statement issued on this session's connection."""
    statements: list[str] = []
    engine = session.get_bind()

    def record(
        _conn,
        _cursor,
        statement,
        _params,
        _context,
        _many,
    ) -> None:  # type: ignore[no-untyped-def]
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", record)
    try:
        yield statements
    finally:
        event.remove(engine, "before_cursor_execute", record)


def make_store(session: Session, slug: str) -> Store:
    """Get or create. The migrations already seed the real retailers, so creating
    "flipkart" unconditionally violates the unique slug constraint."""
    existing = session.execute(select(Store).where(Store.slug == slug)).scalar_one_or_none()
    if existing is not None:
        return existing
    store = Store(
        slug=slug, name=slug.replace("-", " ").title(), domains=[f"{slug}.com"],
        adapter_key="generic", enabled=True,
    )
    session.add(store)
    session.flush()
    return store


def make_product(
    session: Session,
    store: Store,
    *,
    name: str | None,
    price: str | None = "82900.00",
    availability: Availability = Availability.IN_STOCK,
    checked: datetime | None = NOW,
    url: str | None = None,
) -> Product:
    target = url or f"https://{store.slug}.com/{(name or 'item').replace(' ', '-')}"
    product = Product(
        url=target,
        url_canonical=target,
        store_id=store.id,
        name=name,
        current_price=Decimal(price) if price else None,
        currency="INR" if price else None,
        availability=availability,
        last_checked_at=checked,
    )
    session.add(product)
    session.flush()
    return product


def _links_for(session: Session, product_id: int) -> int:
    """How many variants this listing is attached to, across every user's grouping."""
    return int(
        session.execute(
            select(func.count())
            .select_from(VariantListing)
            .where(VariantListing.product_id == product_id)
        ).scalar_one()
    )


def record_check(
    session: Session, product: Product, *, status: CheckStatus, outcome: FetchOutcome | None
) -> None:
    session.add(
        CheckExecution(
            product_id=product.id,
            store_id=product.store_id,
            started_at=NOW,
            status=status,
            fetch_method=FetchMethod.HTTP,
            error_type=outcome.value if outcome else None,
        )
    )
    session.flush()


class TestCreatingGroups:
    def test_slug_is_derived_from_the_name(self, db_session: Session, owner_id: int) -> None:
        group = group_service.create_group(
            db_session,
            user_id=owner_id,
            slug=None,
            name="iPhone 17 Pro",
        )
        assert group.slug == "iphone-17-pro"

    def test_duplicate_slug_is_rejected(self, db_session: Session, owner_id: int) -> None:
        group_service.create_group(db_session, user_id=owner_id, slug="iphone-17", name="iPhone 17")
        with pytest.raises(DuplicateError):
            group_service.create_group(
                db_session,
                user_id=owner_id,
                slug="iphone-17",
                name="Another",
            )

    def test_a_blank_name_is_rejected(self, db_session: Session, owner_id: int) -> None:
        with pytest.raises(ValidationError):
            group_service.create_group(db_session, user_id=owner_id, slug=None, name="   ")

    def test_an_unusable_slug_is_rejected(self, db_session: Session, owner_id: int) -> None:
        with pytest.raises(ValidationError):
            group_service.create_group(db_session, user_id=owner_id, slug="Not A Slug!", name="x")


class TestAttaching:
    def test_two_shops_spelling_a_model_differently_share_one_variant(
        self, db_session: Session, owner_id: int
    ) -> None:
        """The property the comparison grid depends on entirely."""
        group = group_service.create_group(
            db_session,
            user_id=owner_id,
            slug="iphone-17",
            name="iPhone 17",
        )
        flipkart = make_product(
            db_session, make_store(db_session, "flipkart"), name="Apple iPhone 17 (Black, 256 GB)"
        )
        reliance = make_product(
            db_session, make_store(db_session, "reliance"), name="Apple iPhone 17 256 GB, Black"
        )

        _, first = group_service.attach_product(
            db_session,
            flipkart.id,
            user_id=owner_id,
            group_slug=group.slug,
        )
        _, second = group_service.attach_product(
            db_session,
            reliance.id,
            user_id=owner_id,
            group_slug=group.slug,
        )

        assert first.id == second.id
        assert first.label == "256GB / Black"
        variants = db_session.execute(select(func.count()).select_from(ProductVariant))
        assert variants.scalar_one() == 1

    def test_a_title_with_no_clues_is_refused_rather_than_guessed(
        self, db_session: Session, owner_id: int
    ) -> None:
        group = group_service.create_group(db_session, user_id=owner_id, slug="misc", name="Misc")
        product = make_product(
            db_session, make_store(db_session, "shop"), name="Mystery Item",
            url="https://shop.com/thing/p/1",
        )
        with pytest.raises(ValidationError, match="could not infer"):
            group_service.attach_product(
                db_session,
                product.id,
                user_id=owner_id,
                group_slug=group.slug,
            )

    def test_the_url_slug_is_used_when_there_is_no_title(
        self,
        db_session: Session,
        owner_id: int,
    ) -> None:
        """A blocked shop leaves no title, but the retailer still names the model in the path."""
        group = group_service.create_group(
            db_session,
            user_id=owner_id,
            slug="iphone-17",
            name="iPhone 17",
        )
        product = make_product(
            db_session, make_store(db_session, "croma"), name=None, price=None,
            url="https://www.croma.com/apple-iphone-17-256gb-black-/p/317396",
        )
        _, variant = group_service.attach_product(
            db_session,
            product.id,
            user_id=owner_id,
            group_slug=group.slug,
        )
        assert variant.label == "256GB / Black"

    def test_an_explicit_label_overrides_inference(
        self,
        db_session: Session,
        owner_id: int,
    ) -> None:
        group = group_service.create_group(
            db_session,
            user_id=owner_id,
            slug="iphone-17",
            name="iPhone 17",
        )
        product = make_product(
            db_session, make_store(db_session, "flipkart"), name="Apple iPhone 17 (Black, 256 GB)"
        )
        _, variant = group_service.attach_product(
            db_session, product.id, user_id=owner_id, group_slug=group.slug, label="My Own Label"
        )
        assert variant.label == "My Own Label"

    def test_attaching_an_unknown_product_is_a_not_found(
        self,
        db_session: Session,
        owner_id: int,
    ) -> None:
        group_service.create_group(db_session, user_id=owner_id, slug="g", name="G")
        with pytest.raises(NotFoundError):
            group_service.attach_product(db_session, 99999, user_id=owner_id, group_slug="g")

    def test_detaching_leaves_the_listing_tracked(self, db_session: Session, owner_id: int) -> None:
        group = group_service.create_group(
            db_session,
            user_id=owner_id,
            slug="iphone-17",
            name="iPhone 17",
        )
        product = make_product(
            db_session, make_store(db_session, "flipkart"), name="Apple iPhone 17 (Black, 256 GB)"
        )
        group_service.attach_product(
            db_session,
            product.id,
            user_id=owner_id,
            group_slug=group.slug,
        )
        detached = group_service.detach_product(db_session, product.id, owner_id)

        assert _links_for(db_session, detached.id) == 0
        assert db_session.get(Product, product.id) is not None


class TestDeletingAGroupIsSafe:
    """Grouping is an overlay. Removing it must cost nothing but the grouping."""

    def test_listings_and_price_history_survive(self, db_session: Session, owner_id: int) -> None:
        group = group_service.create_group(
            db_session,
            user_id=owner_id,
            slug="iphone-17",
            name="iPhone 17",
        )
        product = make_product(
            db_session, make_store(db_session, "flipkart"), name="Apple iPhone 17 (Black, 256 GB)"
        )
        group_service.attach_product(
            db_session,
            product.id,
            user_id=owner_id,
            group_slug=group.slug,
        )
        db_session.add(
            PriceHistory(
                product_id=product.id, price=Decimal("82900.00"), currency="INR", observed_at=NOW
            )
        )
        db_session.flush()

        group_service.delete_group(db_session, owner_id, "iphone-17")
        db_session.flush()
        db_session.expire_all()

        survivor = db_session.get(Product, product.id)
        assert survivor is not None
        # The listing keeps tracking; it has simply lost its grouping.
        assert _links_for(db_session, survivor.id) == 0
        history = db_session.execute(
            select(func.count()).select_from(PriceHistory).where(
                PriceHistory.product_id == product.id
            )
        ).scalar_one()
        assert history == 1

    def test_variants_are_removed_with_their_group(
        self,
        db_session: Session,
        owner_id: int,
    ) -> None:
        group = group_service.create_group(
            db_session,
            user_id=owner_id,
            slug="iphone-17",
            name="iPhone 17",
        )
        product = make_product(
            db_session, make_store(db_session, "flipkart"), name="Apple iPhone 17 (Black, 256 GB)"
        )
        group_service.attach_product(
            db_session,
            product.id,
            user_id=owner_id,
            group_slug=group.slug,
        )

        group_service.delete_group(db_session, owner_id, "iphone-17")
        db_session.flush()

        remaining = db_session.execute(
            select(func.count()).select_from(ProductVariant)
        ).scalar_one()
        assert remaining == 0


class TestTheGrid:
    @staticmethod
    def build_iphone_group(session: Session, owner_id: int) -> ProductGroup:
        group = group_service.create_group(
            session, user_id=owner_id, slug="iphone-17", name="iPhone 17", brand="Apple"
        )
        shops = {slug: make_store(session, slug) for slug in ("flipkart", "reliance", "croma")}

        black_flipkart = make_product(
            session, shops["flipkart"], name="Apple iPhone 17 (Black, 256 GB)", price="82900.00"
        )
        black_reliance = make_product(
            session, shops["reliance"], name="Apple iPhone 17 256 GB, Black", price="83500.00"
        )
        # Blocked: no title, no price, and the last check says so.
        black_croma = make_product(
            session, shops["croma"], name=None, price=None,
            availability=Availability.UNKNOWN,
            url="https://www.croma.com/apple-iphone-17-256gb-black-/p/317396",
        )
        sage_flipkart = make_product(
            session, shops["flipkart"], name="Apple iPhone 17 (Sage, 256 GB)", price="82900.00",
            availability=Availability.OUT_OF_STOCK,
        )

        for product in (black_flipkart, black_reliance, black_croma, sage_flipkart):
            group_service.attach_product(
                session,
                product.id,
                user_id=owner_id,
                group_slug=group.slug,
            )

        record_check(session, black_flipkart, status=CheckStatus.SUCCESS, outcome=None)
        record_check(session, black_reliance, status=CheckStatus.SUCCESS, outcome=None)
        record_check(session, black_croma, status=CheckStatus.FAILED, outcome=FetchOutcome.BLOCKED)
        record_check(session, sage_flipkart, status=CheckStatus.SUCCESS, outcome=None)
        return group

    def test_shape_is_models_by_shops(self, db_session: Session, owner_id: int) -> None:
        self.build_iphone_group(db_session, owner_id)
        matrix = build_matrix(db_session, "iphone-17", user_id=owner_id, now=NOW)

        assert {row.label for row in matrix.rows} == {"256GB / Black", "256GB / Sage"}
        assert set(matrix.store_slugs) == {"flipkart", "reliance", "croma"}

    def test_each_kind_of_absence_is_reported_distinctly(
        self,
        db_session: Session,
        owner_id: int,
    ) -> None:
        """The point of the whole feature: four blanks, four different meanings."""
        self.build_iphone_group(db_session, owner_id)
        matrix = build_matrix(db_session, "iphone-17", user_id=owner_id, now=NOW)
        rows = {row.label: row for row in matrix.rows}

        black, sage = rows["256GB / Black"], rows["256GB / Sage"]
        assert black.cells["flipkart"].status is CellStatus.OK
        assert black.cells["croma"].status is CellStatus.BLOCKED
        assert sage.cells["flipkart"].status is CellStatus.OUT_OF_STOCK
        # Nobody tracks the Sage at Reliance -- which is not a claim about stock.
        assert sage.cells["reliance"].status is CellStatus.NOT_TRACKED

    def test_best_price_picks_the_cheaper_shop(self, db_session: Session, owner_id: int) -> None:
        self.build_iphone_group(db_session, owner_id)
        matrix = build_matrix(db_session, "iphone-17", user_id=owner_id, now=NOW)
        black = next(row for row in matrix.rows if row.label == "256GB / Black")

        assert black.best_price == Decimal("82900.00")
        assert black.best_store_slugs == ("flipkart",)
        assert black.spread == Decimal("600.00")

    def test_previous_price_drives_the_movement_arrow(
        self,
        db_session: Session,
        owner_id: int,
    ) -> None:
        group = self.build_iphone_group(db_session, owner_id)
        product = db_session.execute(
            select(Product).where(Product.store_id == db_session.execute(
                select(Store.id).where(Store.slug == "reliance")
            ).scalar_one())
        ).scalar_one()
        db_session.add_all(
            [
                PriceHistory(
                    product_id=product.id, price=Decimal("85000.00"), currency="INR",
                    observed_at=NOW - timedelta(days=2),
                ),
                PriceHistory(
                    product_id=product.id, price=Decimal("83500.00"), currency="INR",
                    observed_at=NOW - timedelta(days=1),
                ),
            ]
        )
        db_session.flush()

        matrix = build_matrix(db_session, group.slug, user_id=owner_id, now=NOW)
        cell = next(r for r in matrix.rows if r.label == "256GB / Black").cells["reliance"]
        assert cell.previous_price == Decimal("85000.00")
        assert cell.price_delta == Decimal("-1500.00")

    def test_stale_prices_are_flagged(self, db_session: Session, owner_id: int) -> None:
        self.build_iphone_group(db_session, owner_id)
        matrix = build_matrix(
            db_session, "iphone-17", user_id=owner_id, stale_after=timedelta(
                hours=6,
            ), now=NOW + timedelta(days=3)
        )
        cell = next(r for r in matrix.rows if r.label == "256GB / Black").cells["flipkart"]
        assert cell.is_stale
        assert cell.status is CellStatus.OK

    def test_an_unknown_slug_raises(self, db_session: Session, owner_id: int) -> None:
        with pytest.raises(GroupNotFoundError):
            build_matrix(db_session, "no-such-group", user_id=owner_id)

    def test_grid_loads_in_a_fixed_number_of_queries(
        self,
        db_session: Session,
        owner_id: int,
    ) -> None:
        """Guards against the obvious N+1 rewrite.

        Four listings across two models load in the same four queries as four hundred
        would: variants, listings, previous prices, last checks.
        """
        group = self.build_iphone_group(db_session, owner_id)
        db_session.expire_all()

        repo = GroupRepository(db_session)
        loaded = repo.get_by_slug(owner_id, group.slug)
        assert loaded is not None
        with counted_queries(db_session) as statements:
            repo.load_grid(loaded)

        selects = [s for s in statements if s.lstrip().upper().startswith("SELECT")]
        assert len(selects) == 4, "\n\n".join(selects)
