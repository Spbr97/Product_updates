"""Rule evaluators.

Evaluators are pure functions of a context, so these tests use plain values -- no
database, no clock, no network.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from product_tracker.db.models import TrackingRule
from product_tracker.domain.enums import Availability, RuleType, TrackingStatus
from product_tracker.domain.errors import ValidationError
from product_tracker.domain.models import ProductSnapshot, RuleContext
from product_tracker.services.rules_engine import (
    RULE_EVALUATORS,
    dedupe_signature,
    evaluate,
    evaluate_all,
    validate_params,
)

NOW = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)


def product(price: str | None = "100") -> ProductSnapshot:
    return ProductSnapshot(
        id=1,
        url="https://shop.example.com/p/1",
        store_slug="generic",
        name="Test Product",
        current_price=Decimal(price) if price else None,
        currency="INR",
        availability=Availability.IN_STOCK,
        tracking_status=TrackingStatus.ACTIVE,
        last_checked_at=NOW,
    )


def rule(
    rule_type: RuleType,
    *,
    params: dict | None = None,
    enabled: bool = True,
    cooldown: int | None = None,
    last_fired: datetime | None = None,
    provider: str | None = None,
) -> TrackingRule:
    instance = TrackingRule(
        product_id=1,
        rule_type=rule_type,
        params=params or {},
        enabled=enabled,
        cooldown_seconds=cooldown,
        last_fired_at=last_fired,
        notify_provider=provider,
    )
    instance.id = 10
    return instance


def context(
    previous: str | None = "100",
    current: str | None = "90",
    previous_availability: Availability = Availability.IN_STOCK,
    current_availability: Availability = Availability.IN_STOCK,
) -> RuleContext:
    return RuleContext(
        product=product(current),
        previous_price=Decimal(previous) if previous else None,
        current_price=Decimal(current) if current else None,
        previous_availability=previous_availability,
        current_availability=current_availability,
        observed_at=NOW,
    )


class TestCoverage:
    def test_every_rule_type_has_an_evaluator(self) -> None:
        """A rule type users can create but nothing evaluates would silently never fire."""
        assert set(RULE_EVALUATORS) == set(RuleType)


class TestPriceChanged:
    def test_fires_on_a_drop(self) -> None:
        match = evaluate(rule(RuleType.PRICE_CHANGED), context("100", "90"), now=NOW)
        assert match is not None
        assert "dropped" in match.title

    def test_fires_on_a_rise(self) -> None:
        match = evaluate(rule(RuleType.PRICE_CHANGED), context("100", "110"), now=NOW)
        assert match is not None
        assert "rose" in match.title

    def test_silent_when_unchanged(self) -> None:
        assert evaluate(rule(RuleType.PRICE_CHANGED), context("100", "100"), now=NOW) is None

    def test_silent_on_first_observation(self) -> None:
        """Nothing to compare against yet."""
        assert evaluate(rule(RuleType.PRICE_CHANGED), context(None, "100"), now=NOW) is None


class TestPriceDropped:
    def test_fires_on_a_drop(self) -> None:
        match = evaluate(rule(RuleType.PRICE_DROPPED), context("100", "90"), now=NOW)
        assert match is not None
        assert "Price drop" in match.title
        assert "10.0%" in match.body or "-10.0%" in match.body

    @pytest.mark.parametrize(("previous", "current"), [("100", "110"), ("100", "100")])
    def test_silent_otherwise(self, previous: str, current: str) -> None:
        assert evaluate(rule(RuleType.PRICE_DROPPED), context(previous, current), now=NOW) is None

    def test_silent_on_first_observation(self) -> None:
        assert evaluate(rule(RuleType.PRICE_DROPPED), context(None, "90"), now=NOW) is None


class TestPriceIncreased:
    def test_fires_on_a_rise(self) -> None:
        match = evaluate(rule(RuleType.PRICE_INCREASED), context("100", "110"), now=NOW)
        assert match is not None
        assert "increase" in match.title.lower()

    @pytest.mark.parametrize(("previous", "current"), [("100", "90"), ("100", "100")])
    def test_silent_otherwise(self, previous: str, current: str) -> None:
        assert (
            evaluate(rule(RuleType.PRICE_INCREASED), context(previous, current), now=NOW) is None
        )


class TestPriceBelowTarget:
    def _rule(self, target: str) -> TrackingRule:
        return rule(RuleType.PRICE_BELOW_TARGET, params={"target_price": target})

    def test_fires_below_target(self) -> None:
        match = evaluate(self._rule("95"), context("100", "90"), now=NOW)
        assert match is not None
        assert "Target price reached" in match.title

    def test_fires_exactly_at_target(self) -> None:
        """"at or below" -- the boundary counts."""
        assert evaluate(self._rule("90"), context("100", "90"), now=NOW) is not None

    def test_silent_above_target(self) -> None:
        assert evaluate(self._rule("80"), context("100", "90"), now=NOW) is None

    def test_fires_without_a_previous_price(self) -> None:
        """Someone setting a target after a drop they missed must still be told."""
        assert evaluate(self._rule("95"), context(None, "90"), now=NOW) is not None

    def test_silent_without_a_target(self) -> None:
        assert evaluate(rule(RuleType.PRICE_BELOW_TARGET), context("100", "90"), now=NOW) is None

    def test_silent_with_an_unparseable_target(self) -> None:
        broken = rule(RuleType.PRICE_BELOW_TARGET, params={"target_price": "cheap"})
        assert evaluate(broken, context("100", "90"), now=NOW) is None

    def test_records_the_target_in_context(self) -> None:
        match = evaluate(self._rule("95"), context("100", "90"), now=NOW)
        assert match is not None
        assert match.context["target_price"] == "95"


class TestBecameAvailable:
    def test_fires_from_out_of_stock(self) -> None:
        match = evaluate(
            rule(RuleType.BECAME_AVAILABLE),
            context(
                previous_availability=Availability.OUT_OF_STOCK,
                current_availability=Availability.IN_STOCK,
            ),
            now=NOW,
        )
        assert match is not None
        assert "Back in stock" in match.title

    def test_fires_from_unavailable(self) -> None:
        assert (
            evaluate(
                rule(RuleType.BECAME_AVAILABLE),
                context(
                    previous_availability=Availability.UNAVAILABLE,
                    current_availability=Availability.IN_STOCK,
                ),
                now=NOW,
            )
            is not None
        )

    def test_silent_from_unknown(self) -> None:
        """We never established it was unavailable, so "it's back" would be an invention."""
        assert (
            evaluate(
                rule(RuleType.BECAME_AVAILABLE),
                context(
                    previous_availability=Availability.UNKNOWN,
                    current_availability=Availability.IN_STOCK,
                ),
                now=NOW,
            )
            is None
        )

    def test_silent_when_already_in_stock(self) -> None:
        assert evaluate(rule(RuleType.BECAME_AVAILABLE), context(), now=NOW) is None


class TestBecameUnavailable:
    @pytest.mark.parametrize(
        "current", [Availability.OUT_OF_STOCK, Availability.UNAVAILABLE]
    )
    def test_fires_leaving_stock(self, current: Availability) -> None:
        match = evaluate(
            rule(RuleType.BECAME_UNAVAILABLE),
            context(
                previous_availability=Availability.IN_STOCK, current_availability=current
            ),
            now=NOW,
        )
        assert match is not None
        assert "Out of stock" in match.title

    def test_silent_from_unknown(self) -> None:
        assert (
            evaluate(
                rule(RuleType.BECAME_UNAVAILABLE),
                context(
                    previous_availability=Availability.UNKNOWN,
                    current_availability=Availability.OUT_OF_STOCK,
                ),
                now=NOW,
            )
            is None
        )

    def test_silent_going_to_unknown(self) -> None:
        """Losing track of stock is not the same as going out of stock."""
        assert (
            evaluate(
                rule(RuleType.BECAME_UNAVAILABLE),
                context(
                    previous_availability=Availability.IN_STOCK,
                    current_availability=Availability.UNKNOWN,
                ),
                now=NOW,
            )
            is None
        )


class TestGating:
    def test_disabled_rules_never_fire(self) -> None:
        assert evaluate(rule(RuleType.PRICE_DROPPED, enabled=False), context(), now=NOW) is None

    def test_cooldown_suppresses(self) -> None:
        recent = rule(RuleType.PRICE_DROPPED, cooldown=3600, last_fired=NOW - timedelta(minutes=5))
        assert evaluate(recent, context(), now=NOW) is None

    def test_fires_once_the_cooldown_elapses(self) -> None:
        stale = rule(RuleType.PRICE_DROPPED, cooldown=3600, last_fired=NOW - timedelta(hours=2))
        assert evaluate(stale, context(), now=NOW) is not None

    def test_cooldown_without_a_previous_firing_does_not_suppress(self) -> None:
        fresh = rule(RuleType.PRICE_DROPPED, cooldown=3600, last_fired=None)
        assert evaluate(fresh, context(), now=NOW) is not None


class TestEvaluateAll:
    def test_returns_only_matching_rules(self) -> None:
        rules = [
            rule(RuleType.PRICE_DROPPED),
            rule(RuleType.PRICE_INCREASED),
            rule(RuleType.BECAME_AVAILABLE),
        ]

        matches = evaluate_all(rules, context("100", "90"), now=NOW)

        assert [m.rule_type for m in matches] == [RuleType.PRICE_DROPPED]

    def test_empty_rule_set(self) -> None:
        assert evaluate_all([], context(), now=NOW) == []

    def test_carries_the_provider_preference(self) -> None:
        matches = evaluate_all(
            [rule(RuleType.PRICE_DROPPED, provider="telegram")], context("100", "90"), now=NOW
        )
        assert matches[0].context["notify_provider"] == "telegram"


class TestValidateParams:
    def test_target_price_is_required(self) -> None:
        with pytest.raises(ValidationError, match="requires a target_price"):
            validate_params(RuleType.PRICE_BELOW_TARGET, {})

    def test_target_price_must_be_a_number(self) -> None:
        with pytest.raises(ValidationError, match="not a number"):
            validate_params(RuleType.PRICE_BELOW_TARGET, {"target_price": "cheap"})

    @pytest.mark.parametrize("value", ["0", "-5"])
    def test_target_price_must_be_positive(self, value: str) -> None:
        with pytest.raises(ValidationError, match="greater than zero"):
            validate_params(RuleType.PRICE_BELOW_TARGET, {"target_price": value})

    def test_target_price_is_normalised_to_a_string(self) -> None:
        """JSONB would round-trip a float and lose precision."""
        result = validate_params(RuleType.PRICE_BELOW_TARGET, {"target_price": 69999.5})
        assert result["target_price"] == "69999.5"
        assert isinstance(result["target_price"], str)

    def test_other_rule_types_need_no_params(self) -> None:
        assert validate_params(RuleType.PRICE_DROPPED, {}) == {}


class TestDedupeSignature:
    def test_price_rules_key_on_the_transition(self) -> None:
        match = evaluate(rule(RuleType.PRICE_DROPPED), context("100", "90"), now=NOW)
        assert match is not None
        assert dedupe_signature(match) == "100->90"

    def test_availability_rules_key_on_the_resulting_state(self) -> None:
        match = evaluate(
            rule(RuleType.BECAME_UNAVAILABLE),
            context(
                previous_availability=Availability.IN_STOCK,
                current_availability=Availability.OUT_OF_STOCK,
            ),
            now=NOW,
        )
        assert match is not None
        assert dedupe_signature(match) == "out_of_stock"

    def test_different_transitions_differ(self) -> None:
        first = evaluate(rule(RuleType.PRICE_DROPPED), context("100", "90"), now=NOW)
        second = evaluate(rule(RuleType.PRICE_DROPPED), context("100", "80"), now=NOW)
        assert first and second
        assert dedupe_signature(first) != dedupe_signature(second)
