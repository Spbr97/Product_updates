"""Price parsing and formatting.

Money is always :class:`~decimal.Decimal`; float would introduce rounding error into
values we compare for equality when detecting a price change.

Parsing is deliberately strict. Returning ``None`` for something ambiguous is correct
behaviour -- the caller reports ``PRICE_NOT_FOUND`` and the check is recorded as
unsuccessful. Guessing would put a wrong number into permanent price history.
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

#: Currency symbols and words seen on product pages, mapped to ISO 4217 codes.
CURRENCY_SYMBOLS: dict[str, str] = {
    "₹": "INR",
    "rs.": "INR",
    "rs": "INR",
    "inr": "INR",
    "$": "USD",
    "usd": "USD",
    "£": "GBP",
    "gbp": "GBP",
    "€": "EUR",
    "eur": "EUR",
    "¥": "JPY",
    "jpy": "JPY",
    "a$": "AUD",
    "aud": "AUD",
    "c$": "CAD",
    "cad": "CAD",
}

DEFAULT_CURRENCY = "INR"

_ISO_CODE = re.compile(r"^[A-Z]{3}$")
#: Digit-group separators seen in prices. The no-break (U+00A0) and narrow no-break
#: (U+202F) spaces are common in text copied from web pages; they are built with chr()
#: rather than typed literally, because they are invisible in source and easily lost.
_SEPARATORS = r"\s" + chr(0x00A0) + chr(0x202F) + "'"
_DIGITS_AND_SEPARATORS = re.compile(f"[0-9][0-9,.{_SEPARATORS}]*")


def parse_price(value: object) -> Decimal | None:
    """Extract a price from a string or number, or return ``None``.

    Handles the groupings that appear in practice, including Indian lakh grouping
    (``1,23,456.00``) and European style (``1.234,56``). Anything genuinely ambiguous
    returns ``None`` rather than a guess.
    """
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value if value >= 0 else None
    if isinstance(value, int) and not isinstance(value, bool):
        return Decimal(value) if value >= 0 else None
    if isinstance(value, float):
        # Round-trip through str so 69999.0 does not become 69998.99999999999.
        return parse_price(repr(value))

    text = str(value).strip()
    if not text:
        return None

    match = _DIGITS_AND_SEPARATORS.search(text)
    if not match:
        return None

    number = re.sub(f"[{_SEPARATORS}]", "", match.group(0)).rstrip(".,")
    if not number:
        return None

    normalised = _normalise_separators(number)
    if normalised is None:
        return None

    try:
        parsed = Decimal(normalised)
    except InvalidOperation:
        return None
    return parsed if parsed >= 0 else None


def _normalise_separators(number: str) -> str | None:
    """Resolve ',' and '.' into a plain decimal string, or ``None`` if ambiguous."""
    has_comma = "," in number
    has_dot = "." in number

    if not has_comma and not has_dot:
        return number

    if has_comma and has_dot:
        # Whichever appears last is the decimal separator; the other groups digits.
        if number.rindex(",") > number.rindex("."):
            return number.replace(".", "").replace(",", ".")
        return number.replace(",", "")

    separator = "," if has_comma else "."
    parts = number.split(separator)
    head, tail = parts[0], parts[-1]

    if len(parts) > 2:
        # Repeated separator only makes sense as digit grouping (1,23,456 or 1.234.567).
        return "".join(parts) if len(tail) == 3 else None

    if separator == ",":
        # A decimal comma with exactly three places is not a real notation, so ",ddd" is
        # always a thousands group.
        if len(tail) == 3:
            return "".join(parts)
        return f"{head}.{tail}" if len(tail) in (1, 2) else None

    # A single dot. Only genuinely ambiguous when the left side is short enough to be a
    # thousands group: "1.234" could be 1234 (European) or 1.234. "1234.500" could not --
    # European grouping would have written it "1.234.500" -- so that is a decimal, as are
    # values with more than three decimal places.
    if len(tail) == 3 and len(head) <= 3:
        return None
    return f"{head}.{tail}"


def parse_currency(*candidates: object, default: str | None = None) -> str | None:
    """Return the first ISO 4217 code found among the candidates.

    Accepts a code directly (``"INR"``) or infers one from a symbol in a price string
    (``"₹69,999"``).
    """
    for candidate in candidates:
        if candidate is None:
            continue
        text = str(candidate).strip()
        if not text:
            continue

        if _ISO_CODE.match(text.upper()) and text.upper() in _KNOWN_CODES:
            return text.upper()

        lowered = text.lower()
        for symbol, code in CURRENCY_SYMBOLS.items():
            if symbol in lowered:
                return code

    return default


_KNOWN_CODES = set(CURRENCY_SYMBOLS.values())

_SYMBOL_FOR_CODE = {"INR": "₹", "USD": "$", "GBP": "£", "EUR": "€", "JPY": "¥"}


def format_money(amount: Decimal | None, currency: str | None = None) -> str:
    """Render a price for display. ``None`` becomes a readable placeholder."""
    if amount is None:
        return "not listed"
    symbol = _SYMBOL_FOR_CODE.get((currency or "").upper())
    if symbol:
        return f"{symbol}{amount:,.2f}"
    if currency:
        return f"{amount:,.2f} {currency.upper()}"
    return f"{amount:,.2f}"
