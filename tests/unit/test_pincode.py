"""Delivery-area handling: what gets applied, and what gets relabelled.

Two properties carry the whole module, and both are about *not* overclaiming:

* With no PIN code configured, nothing anywhere changes. This feature must be invisible
  until someone asks for it.
* A relabelled result is still a "we could not read a price" result. It never becomes
  out-of-stock, and it never becomes a success.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from product_tracker.domain.enums import Availability, FetchMethod, FetchOutcome
from product_tracker.domain.models import FetchContext, FetchResult
from product_tracker.stores import pincode

WITH_PIN = FetchContext(delivery_pincode="560037")
WITHOUT_PIN = FetchContext()

AMAZON = "https://www.amazon.in/dp/B0TEST1234"
SAMSUNG = "https://www.samsung.com/in/smartphones/galaxy-s25/"
UNKNOWN = "https://shop.example.com/p/1"


def no_price(availability: Availability = Availability.UNKNOWN) -> FetchResult:
    return FetchResult(
        outcome=FetchOutcome.PRICE_NOT_FOUND,
        availability=availability,
        name="A product",
        fetch_method=FetchMethod.HTTP,
        http_status=200,
    )


class TestRuleLookup:
    @pytest.mark.parametrize(
        "url",
        [
            "https://amazon.in/dp/B0X",
            "https://www.amazon.in/dp/B0X",
            "https://smile.amazon.in/dp/B0X",
        ],
    )
    def test_matches_the_domain_and_its_subdomains(self, url: str) -> None:
        assert pincode.rule_for(url) is not None

    def test_does_not_match_a_lookalike_domain(self) -> None:
        """``notamazon.in`` must not inherit amazon.in's classification."""
        assert pincode.rule_for("https://notamazon.in/dp/B0X") is None

    def test_an_unclassified_host_has_no_rule(self) -> None:
        assert pincode.rule_for(UNKNOWN) is None

    def test_a_url_without_a_host_has_no_rule(self) -> None:
        assert pincode.rule_for("not a url") is None

    def test_no_rule_claims_both_classifications(self) -> None:
        """``needs_js`` and ``location_independent`` are contradictory claims."""
        for domain, rule in pincode.RULES.items():
            assert not (rule.needs_js and rule.location_independent), domain

    def test_every_rule_says_why(self) -> None:
        for domain, rule in pincode.RULES.items():
            assert rule.note.strip(), domain


class TestApply:
    def test_is_a_no_op_without_a_pincode(self) -> None:
        assert pincode.apply(AMAZON, WITHOUT_PIN) == (AMAZON, {})

    def test_is_a_no_op_for_an_unclassified_host(self) -> None:
        assert pincode.apply(UNKNOWN, WITH_PIN) == (UNKNOWN, {})

    def test_sends_nothing_for_any_catalogued_host_today(self) -> None:
        """The honest state of the world: no shop can be localised statically.

        If this starts failing because a rule gained a cookie or a parameter, that is the
        feature working -- update it to assert the new mechanism, and note that
        ``escalate`` deliberately stops firing for that host.
        """
        for domain in pincode.RULES:
            url = f"https://www.{domain}/p/1"
            assert pincode.apply(url, WITH_PIN) == (url, {})

    def test_a_cookie_rule_sends_the_bare_pincode(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setitem(
            pincode.RULES, "example.test", pincode.PincodeRule(cookies=("pin",), note="x")
        )

        url, cookies = pincode.apply("https://example.test/p/1", WITH_PIN)

        assert url == "https://example.test/p/1"
        assert cookies == {"pin": "560037"}

    def test_a_query_rule_replaces_an_existing_value(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A URL already carrying the parameter must not end up with two of them."""
        monkeypatch.setitem(
            pincode.RULES, "example.test", pincode.PincodeRule(query_param="pin", note="x")
        )

        url, _ = pincode.apply("https://example.test/p/1?pin=110001&x=1", WITH_PIN)

        assert url == "https://example.test/p/1?x=1&pin=560037"


class TestEscalate:
    def test_does_nothing_without_a_pincode(self) -> None:
        result = no_price()
        assert pincode.escalate(AMAZON, WITHOUT_PIN, result) is result

    def test_does_nothing_for_an_unclassified_host(self) -> None:
        result = no_price()
        assert pincode.escalate(UNKNOWN, WITH_PIN, result) is result

    def test_does_nothing_for_a_national_pricing_shop(self) -> None:
        result = no_price()
        assert pincode.escalate(SAMSUNG, WITH_PIN, result) is result

    def test_relabels_a_price_miss_on_an_area_priced_shop(self) -> None:
        escalated = pincode.escalate(AMAZON, WITH_PIN, no_price())

        assert escalated.outcome is FetchOutcome.NEEDS_LOCATION
        assert "560037" in (escalated.message or "")

    def test_availability_is_carried_through_untouched(self) -> None:
        """The invariant this project exists to protect: a price we could not read is
        never a statement that the product is gone."""
        escalated = pincode.escalate(AMAZON, WITH_PIN, no_price())

        assert escalated.availability is Availability.UNKNOWN
        assert escalated.price is None

    def test_what_the_page_did_say_survives(self) -> None:
        escalated = pincode.escalate(AMAZON, WITH_PIN, no_price(Availability.IN_STOCK))

        assert escalated.availability is Availability.IN_STOCK
        assert escalated.name == "A product"
        assert escalated.http_status == 200

    @pytest.mark.parametrize(
        "outcome",
        [
            FetchOutcome.OUT_OF_STOCK,
            FetchOutcome.BLOCKED,
            FetchOutcome.PAGE_STRUCTURE,
            FetchOutcome.UNAVAILABLE,
        ],
    )
    def test_only_a_price_miss_is_ever_relabelled(self, outcome: FetchOutcome) -> None:
        """A block is a block. Relabelling one as "needs a location" would hide it."""
        result = FetchResult(
            outcome=outcome,
            availability=Availability.UNKNOWN,
            fetch_method=FetchMethod.HTTP,
        )

        assert pincode.escalate(AMAZON, WITH_PIN, result) is result

    def test_a_successful_read_is_never_touched(self) -> None:
        result = FetchResult(
            outcome=FetchOutcome.OK,
            availability=Availability.IN_STOCK,
            price=Decimal("61480.00"),
            currency="INR",
            fetch_method=FetchMethod.HTTP,
        )

        assert pincode.escalate(AMAZON, WITH_PIN, result) is result

    def test_stops_firing_once_a_host_can_be_localised(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A miss on a host we *did* localise is a real miss, not a location problem."""
        monkeypatch.setitem(
            pincode.RULES,
            "amazon.in",
            pincode.PincodeRule(cookies=("pin",), needs_js=True, note="x"),
        )
        result = no_price()

        assert pincode.escalate(AMAZON, WITH_PIN, result) is result


class TestOutcomeClassification:
    def test_needs_location_is_neither_a_success_nor_transient(self) -> None:
        """Not a success: no price was learned. Not transient: retrying in ten seconds
        will produce exactly the same answer, and asking again is what shops object to."""
        assert not FetchOutcome.NEEDS_LOCATION.is_success
        assert not FetchOutcome.NEEDS_LOCATION.is_transient

