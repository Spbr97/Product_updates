"""Applying a declarative tracking file.

This restores the one job the original ``config.yaml`` did that nothing else took over:
saying what an installation watches, in a form a person can read, edit and commit. What
matters most is that re-applying it is safe, because a file kept in version control will
be applied more than once by definition.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
import respx
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from tests.unit.test_adapters import load as load_fixture

from product_tracker.core.config import get_settings
from product_tracker.db.models import Product, TrackingRule
from product_tracker.domain.errors import ValidationError
from product_tracker.services import provisioning
from product_tracker.services.comparison import build_matrix
from product_tracker.stores.registry import StoreRegistry

pytestmark = pytest.mark.db

FLIPKART = "https://www.flipkart.com/apple-iphone-17-black-256-gb/p/itm6eb39da622cdd"
VIJAY = "https://www.vijaysales.com/p/P245179/245183/apple-iphone-17-256gb-sage"


@pytest.fixture(autouse=True)
def _respx_router() -> Iterator[None]:
    with respx.mock:
        for url in (FLIPKART, VIJAY):
            respx.get(url).mock(
                return_value=httpx.Response(200, html=load_fixture("jsonld_in_stock.html"))
            )
        yield


def write(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "tracking.yaml"
    path.write_text(body, encoding="utf-8")
    return path


STARTER = f"""
delivery_pincode: "560037"
products:
  - name: "Apple iPhone 17"
    brand: "Apple"
    group: "iphone-17"
    label: "256GB"
    listings:
      - "{FLIPKART}"
      - "{VIJAY}"
    alerts:
      - type: "price_dropped"
"""


def apply(session: Session, path: Path, user_id: int) -> provisioning.Report:
    return provisioning.apply(
        session,
        provisioning.load(path),
        user_id=user_id,
        settings=get_settings(),
        registry=StoreRegistry(),
    )


class TestApplying:
    def test_tracks_every_listing(
        self, db_session: Session, owner_id: int, tmp_path: Path
    ) -> None:
        report = apply(db_session, write(tmp_path, STARTER), owner_id)

        assert report.tracked == 2
        assert report.failed == 0
        total = db_session.execute(select(func.count()).select_from(Product)).scalar_one()
        assert total == 2

    def test_builds_the_comparison_group(
        self, db_session: Session, owner_id: int, tmp_path: Path
    ) -> None:
        """The point of `group:` -- one command and `compare` works."""
        apply(db_session, write(tmp_path, STARTER), owner_id)

        matrix = build_matrix(db_session, "iphone-17", user_id=owner_id)
        assert matrix.group_name == "Apple iPhone 17"
        assert {"flipkart", "vijay-sales"} <= set(matrix.store_slugs)

    def test_applies_the_alert_rules(
        self, db_session: Session, owner_id: int, tmp_path: Path
    ) -> None:
        report = apply(db_session, write(tmp_path, STARTER), owner_id)

        rules = db_session.execute(
            select(func.count()).select_from(TrackingRule)
        ).scalar_one()
        assert rules == 2  # one per listing
        assert report.products[0].alerts_added == 2


class TestReapplyingIsSafe:
    """A file in version control gets applied more than once by definition."""

    def test_a_second_run_adds_nothing(
        self, db_session: Session, owner_id: int, tmp_path: Path
    ) -> None:
        path = write(tmp_path, STARTER)
        apply(db_session, path, owner_id)

        second = apply(db_session, path, owner_id)

        assert second.tracked == 0
        assert second.already == 2
        assert second.failed == 0
        total = db_session.execute(select(func.count()).select_from(Product)).scalar_one()
        assert total == 2

    def test_a_second_run_does_not_duplicate_alerts(
        self, db_session: Session, owner_id: int, tmp_path: Path
    ) -> None:
        path = write(tmp_path, STARTER)
        apply(db_session, path, owner_id)
        apply(db_session, path, owner_id)

        rules = db_session.execute(
            select(func.count()).select_from(TrackingRule)
        ).scalar_one()
        assert rules == 2


class TestOneBadLineDoesNotAbandonTheRest:
    def test_a_bad_url_is_reported_and_the_others_still_apply(
        self, db_session: Session, owner_id: int, tmp_path: Path
    ) -> None:
        """A run that gives up after the first typo is a worse tool than one that says
        which line to fix."""
        body = STARTER.replace(f'      - "{VIJAY}"', '      - "not-a-url"')

        report = apply(db_session, write(tmp_path, body), owner_id)

        assert report.tracked == 1
        assert report.failed == 1
        bad = [
            listing
            for product in report.products
            for listing in product.listings
            if listing.status == "failed"
        ]
        assert bad[0].url == "not-a-url"
        assert bad[0].detail


class TestValidation:
    """Every message names the field, so a forty-line file does not need bisecting."""

    def test_a_product_without_listings_is_refused(self, tmp_path: Path) -> None:
        body = 'products:\n  - name: "X"\n'
        with pytest.raises(ValidationError, match=r"products\[0\] needs at least one URL"):
            provisioning.load(write(tmp_path, body))

    def test_a_product_without_a_name_is_refused(self, tmp_path: Path) -> None:
        body = 'products:\n  - listings: ["https://x.example.com/p/1"]\n'
        with pytest.raises(ValidationError, match=r"products\[0\] needs a name"):
            provisioning.load(write(tmp_path, body))

    def test_an_unknown_alert_type_lists_the_real_ones(self, tmp_path: Path) -> None:
        body = (
            'products:\n  - name: "X"\n    listings: ["https://x.example.com/p/1"]\n'
            '    alerts:\n      - type: "price_halved"\n'
        )
        with pytest.raises(ValidationError, match="price_dropped"):
            provisioning.load(write(tmp_path, body))

    def test_a_target_rule_without_a_target_is_refused(self, tmp_path: Path) -> None:
        """Saved without one it would never fire, which is worse than being refused."""
        body = (
            'products:\n  - name: "X"\n    listings: ["https://x.example.com/p/1"]\n'
            '    alerts:\n      - type: "price_below_target"\n'
        )
        with pytest.raises(ValidationError, match="needs a 'target'"):
            provisioning.load(write(tmp_path, body))

    def test_malformed_yaml_names_the_file(self, tmp_path: Path) -> None:
        with pytest.raises(ValidationError, match="not valid YAML"):
            provisioning.load(write(tmp_path, "products: [oops\n"))


class TestPincodeAdvice:
    """The file declares a delivery area; the environment owns it.

    Adopting it silently would give one setting two sources, and the loser would be
    invisible. So this reports, and never writes.
    """

    def test_it_says_what_to_set_when_unset(
        self, db_session: Session, owner_id: int, tmp_path: Path
    ) -> None:
        report = apply(db_session, write(tmp_path, STARTER), owner_id)

        assert report.pincode_advice is not None
        assert "DELIVERY_PINCODE=560037" in report.pincode_advice

    def test_silent_when_the_environment_already_agrees(
        self, db_session: Session, owner_id: int, tmp_path: Path
    ) -> None:
        settings = get_settings().model_copy(update={"delivery_pincode": "560037"})

        report = provisioning.apply(
            db_session,
            provisioning.load(write(tmp_path, STARTER)),
            user_id=owner_id,
            settings=settings,
            registry=StoreRegistry(),
        )

        assert report.pincode_advice is None

    def test_a_disagreement_says_which_one_wins(
        self, db_session: Session, owner_id: int, tmp_path: Path
    ) -> None:
        settings = get_settings().model_copy(update={"delivery_pincode": "110001"})

        report = provisioning.apply(
            db_session,
            provisioning.load(write(tmp_path, STARTER)),
            user_id=owner_id,
            settings=settings,
            registry=StoreRegistry(),
        )

        assert report.pincode_advice is not None
        assert "environment wins" in report.pincode_advice


class TestTheShippedExample:
    """The starter file is the first thing a new user runs. It must be valid."""

    def test_it_parses_and_declares_the_brief_s_product(self) -> None:
        # Anchored to the repo, not the working directory: pytest may be run from
        # anywhere, and a test that only passes from the root is a flaky test.
        example = Path(__file__).resolve().parents[2] / "tracking.example.yaml"
        plan = provisioning.load(example)

        assert plan.delivery_pincode == "560037"
        assert plan.products[0]["name"] == "Apple iPhone 17"
        # The five retailers the original config.yaml listed directly.
        assert len(plan.products[0]["listings"]) == 5
