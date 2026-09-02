"""What search will and will not accept as a query.

The rule exists because of a measurement, not a preference. Every sanctioned discovery
route -- a shop's sitemap, a shop's published browse listing -- is ordered by the shop,
not by relevance to a question we asked. On Flipkart's Samsung phone listing the Galaxy
S25 was not on page one at all; it appeared on page two, behind older models. A query
naming a model can page towards it. A query naming a category has nothing to page towards,
so "phone" would return the first forty-four phones a shop happens to feature, dressed up
as an answer.
"""

from __future__ import annotations

import pytest

from product_tracker.domain.errors import ValidationError
from product_tracker.services.query_policy import (
    is_specific,
    refusal_reason,
    require_specific,
)


class TestQueriesThatNameAProduct:
    @pytest.mark.parametrize(
        "query",
        [
            "Galaxy S25",
            "iPhone 17",
            "boAt Airdopes 311 Pro",
            "Anker 737 Power Bank",
            "Galaxy Buds3 Pro",
            "Sony WH-1000XM5",
            "AirPods Pro Max",  # No digit, but three meaningful words.
            "Samsung Galaxy S25 Ultra 512GB",
        ],
    )
    def test_accepted(self, query: str) -> None:
        assert is_specific(query), refusal_reason(query)

    def test_a_model_number_is_enough_on_its_own(self) -> None:
        """One digit-bearing token outweighs everything else in the query."""
        assert is_specific("power bank 20000mah")


class TestQueriesThatNameACategory:
    @pytest.mark.parametrize(
        "query",
        ["phone", "phones", "powerbank", "earbuds", "laptop", "smartphone", "headphones"],
    )
    def test_a_bare_category_is_refused(self, query: str) -> None:
        assert not is_specific(query)

    def test_the_refusal_says_what_to_do_instead(self) -> None:
        reason = refusal_reason("earbuds")
        assert reason is not None
        # It must point at the link box, which is the tool for browsing a category.
        assert "paste" in reason.lower()
        assert "link" in reason.lower()

    def test_a_brand_and_a_category_is_still_not_a_product(self) -> None:
        """'Samsung phone' identifies a few hundred products, which is not one."""
        assert not is_specific("Samsung phone")

    def test_shopping_filler_does_not_make_a_query_specific(self) -> None:
        assert not is_specific("best power bank")
        assert not is_specific("buy cheap earbuds online in india")

    def test_an_empty_query_is_refused_without_crashing(self) -> None:
        assert not is_specific("")
        assert not is_specific("   ")
        assert not is_specific("!!!")


class TestRequireSpecific:
    def test_it_raises_for_a_category(self) -> None:
        with pytest.raises(ValidationError, match="category"):
            require_specific("phone")

    def test_it_is_silent_for_a_product(self) -> None:
        require_specific("Galaxy S25")

    def test_the_message_reaches_the_caller_intact(self) -> None:
        """The API renders this as a 422 body and the CLI prints it, so it is user-facing."""
        with pytest.raises(ValidationError) as raised:
            require_specific("laptop")
        assert str(raised.value) == refusal_reason("laptop")
