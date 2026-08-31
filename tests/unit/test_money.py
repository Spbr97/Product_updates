"""Price parsing and formatting."""

from __future__ import annotations

from decimal import Decimal

import pytest

from product_tracker.utils.money import format_money, parse_currency, parse_price


class TestParsePrice:
    @pytest.mark.parametrize(
        ("given", "expected"),
        [
            ("69999", "69999"),
            ("69999.00", "69999.00"),
            ("69,999", "69999"),
            ("69,999.00", "69999.00"),
            ("₹69,999.00", "69999.00"),
            ("Rs. 69,999", "69999"),
            ("INR 69999", "69999"),
            ("  ₹ 1,299.50  ", "1299.50"),
            ("$1,299.99", "1299.99"),
            ("1 299,50", "1299.50"),
        ],
    )
    def test_parses_common_formats(self, given: str, expected: str) -> None:
        assert parse_price(given) == Decimal(expected)

    def test_indian_lakh_grouping(self) -> None:
        """1,23,456 is 123456 -- not something a naive comma-strip gets wrong, but pinned."""
        assert parse_price("₹1,23,456.00") == Decimal("123456.00")

    def test_european_decimal_comma(self) -> None:
        assert parse_price("1.234,56") == Decimal("1234.56")

    @pytest.mark.parametrize(
        ("given", "expected"),
        [(0, "0"), (69999, "69999"), (Decimal("12.34"), "12.34"), (69999.0, "69999.0")],
    )
    def test_accepts_numeric_types(self, given: object, expected: str) -> None:
        assert parse_price(given) == Decimal(expected)

    def test_float_does_not_pick_up_binary_noise(self) -> None:
        assert parse_price(0.1 + 0.2) == Decimal("0.30000000000000004")

    @pytest.mark.parametrize("given", [None, "", "   ", "out of stock", "N/A", "Price on request"])
    def test_returns_none_for_non_prices(self, given: object) -> None:
        assert parse_price(given) is None

    @pytest.mark.parametrize("given", ["1.234", "12.345", "123.456"])
    def test_refuses_ambiguous_single_dot_grouping(self, given: str) -> None:
        """'1.234' is 1234 in Europe and 1.234 elsewhere. Refuse rather than guess."""
        assert parse_price(given) is None

    @pytest.mark.parametrize(
        ("given", "expected"),
        [("1234.500", "1234.500"), ("69999.000", "69999.000"), ("12345.6789", "12345.6789")],
    )
    def test_long_integer_part_disambiguates_a_dot(self, given: str, expected: str) -> None:
        """European grouping would write 1234500 as '1.234.500', so '1234.500' is decimal."""
        assert parse_price(given) == Decimal(expected)

    def test_rejects_negative(self) -> None:
        assert parse_price(-100) is None
        assert parse_price(Decimal("-1")) is None

    def test_booleans_are_not_prices(self) -> None:
        assert parse_price(True) is None

    def test_never_raises(self) -> None:
        for value in ["...", ",,,", "1.2.3.4.5", "٣٤٥", object()]:
            parse_price(value)


class TestParseCurrency:
    @pytest.mark.parametrize(
        ("given", "expected"),
        [("INR", "INR"), ("inr", "INR"), ("USD", "USD"), ("₹69,999", "INR"), ("$12.00", "USD")],
    )
    def test_detects_code_or_symbol(self, given: str, expected: str) -> None:
        assert parse_currency(given) == expected

    def test_first_match_wins(self) -> None:
        assert parse_currency(None, "", "INR", "USD") == "INR"

    def test_falls_back_to_default(self) -> None:
        assert parse_currency(None, "nothing here", default="INR") == "INR"

    def test_returns_none_without_default(self) -> None:
        assert parse_currency("nothing here") is None

    def test_ignores_unknown_three_letter_words(self) -> None:
        """'THE' is not a currency."""
        assert parse_currency("THE") is None


class TestFormatMoney:
    @pytest.mark.parametrize(
        ("amount", "currency", "expected"),
        [
            (Decimal("69999"), "INR", "₹69,999.00"),
            (Decimal("1299.5"), "USD", "$1,299.50"),
            (Decimal("100"), "AUD", "100.00 AUD"),
            (Decimal("100"), None, "100.00"),
        ],
    )
    def test_formats_with_symbol_or_code(
        self, amount: Decimal, currency: str | None, expected: str
    ) -> None:
        assert format_money(amount, currency) == expected

    def test_missing_price_is_readable(self) -> None:
        assert format_money(None, "INR") == "not listed"


class TestRoundTrip:
    @pytest.mark.parametrize("raw", ["₹69,999.00", "₹1,23,456.00", "$1,299.99"])
    def test_parse_then_format_is_stable(self, raw: str) -> None:
        price = parse_price(raw)
        assert price is not None
        currency = parse_currency(raw)
        assert parse_price(format_money(price, currency)) == price
