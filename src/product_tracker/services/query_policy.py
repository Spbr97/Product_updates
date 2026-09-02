"""What search is for, and what it is not for.

Search identifies *one product* across shops so its prices can be compared. It is not a
way to browse a category, and the difference is not a matter of taste -- it is what the
retailers' own catalogues can and cannot answer.

A shop's sanctioned discovery routes are its sitemap and its published browse pages. Both
are ordered by the shop, not by relevance to a question we asked. Searching "Galaxy S25"
against them works, because the model name is in the URL slug and in the card title.
Searching "phone" cannot work: a browse page for phones is sorted by popularity and
several hundred long, so the honest answer to "phone" is the first forty-four phones
Flipkart happens to feature, which is not an answer to anything.

Measured, not assumed: on Flipkart's Samsung phone browse page, the Galaxy S25 was not on
page one at all. It appeared on page two, behind older models. A specific query justifies
paging until the model appears. A generic one has nothing to page towards.

So a query must name a product. When it does not, this refuses with a message that says
what to do instead -- paste the product's link, which needs no search at all.
"""

from __future__ import annotations

import re

from ..domain.errors import ValidationError

_TOKEN = re.compile(r"[a-z0-9]+")

#: Words that describe *a kind of thing*, never a particular one. A query made only of
#: these is a category, and a category is what the link box is for.
CATEGORY_WORDS: frozenset[str] = frozenset(
    {
        "phone", "phones", "smartphone", "smartphones", "mobile", "mobiles",
        "earbud", "earbuds", "earphone", "earphones", "headphone", "headphones",
        "headset", "tws", "airdopes", "buds",
        "powerbank", "powerbanks", "charger", "chargers", "adapter",
        "laptop", "laptops", "tablet", "tablets", "watch", "watches",
        "tv", "television", "speaker", "speakers", "camera", "cameras",
        "monitor", "monitors", "keyboard", "mouse", "printer",
        "refrigerator", "fridge", "washing", "machine", "ac", "cooler",
    }
)

#: Shopping filler. Present in how people type, absent from what they mean.
_FILLER: frozenset[str] = frozenset(
    {
        "best", "top", "new", "latest", "cheap", "cheapest", "good",
        "buy", "online", "price", "prices", "deal", "deals", "offer", "offers",
        "in", "india", "the", "a", "an", "for", "with", "and", "under", "below",
        "rs", "inr", "sale", "discount",
    }
)

#: A query with no digit needs at least this many meaningful words to name a product.
#: "AirPods Pro Max" reaches it; "Samsung phone" does not, and should not.
_MIN_WORDS_WITHOUT_A_NUMBER = 3


def _meaningful(query: str) -> list[str]:
    """Query words with shopping filler removed."""
    return [word for word in _TOKEN.findall(query.lower()) if word not in _FILLER]


def is_specific(query: str) -> bool:
    """Whether this query names a particular product rather than a kind of product."""
    return refusal_reason(query) is None


def refusal_reason(query: str) -> str | None:
    """Why this query cannot be searched, or ``None`` when it can.

    The message is written to be shown to a person, because it will be.
    """
    words = _meaningful(query)
    if not words:
        return (
            "that is not a product name. Search finds one particular product across the "
            "shops -- try a model, like 'Galaxy S25' or 'boAt Airdopes 311'."
        )

    # A model number is the strongest possible signal: S25, 311, 737, WH-1000XM5, 5000mAh.
    # One is enough on its own, whatever else the query contains.
    if any(any(character.isdigit() for character in word) for word in words):
        return None

    distinctive = [word for word in words if word not in CATEGORY_WORDS]
    if not distinctive:
        named = ", ".join(sorted(set(words)))
        return (
            f"'{named}' names a category, not a product. Search compares one product "
            "across shops; it cannot rank a whole category, because shops publish their "
            "catalogues in their own order rather than by relevance to a question. "
            "To track something from a category page, open the shop, find the product, "
            "and paste its link instead."
        )

    if len(words) < _MIN_WORDS_WITHOUT_A_NUMBER:
        return (
            f"'{query.strip()}' is not specific enough to identify one product. Add the "
            "model -- 'Galaxy S25' rather than 'Samsung phone' -- or paste the product's "
            "link directly."
        )

    return None


def require_specific(query: str) -> None:
    """Raise :class:`ValidationError` unless the query names a product.

    A ``ValidationError`` because it is one: the request is well-formed but asks for
    something search cannot honestly do. The API renders it as a 422 with this message,
    and the CLI prints it.
    """
    reason = refusal_reason(query)
    if reason is not None:
        raise ValidationError(reason)
