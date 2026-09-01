"""Working out which model a listing is, from its title.

Stores do not publish a variant identifier we can rely on, so the title is what we have:

    "Apple iPhone 17 (256 GB) - Lavender"
    "Apple iPhone 17 256GB Lavender"
    "APPLE iPhone 17 (Lavender, 256 GB)"

All three are one variant. Reading them as three would split a model across shops and make
the comparison grid useless, which is the whole reason the variant table exists.

*What* distinguishes one model from another depends on the kind of product, so the reading
itself lives in :mod:`specs`, one profile per category. This module is the part that turns
what was read into a stable label -- and the part that knows when to give up.

That last point is deliberate. This is a *suggestion engine*: it proposes a label and a
human confirms it. Inference that silently mis-groups two products is worse than no
inference, so anything it cannot read with confidence it leaves out, and an empty result is
a perfectly good answer.
"""

from __future__ import annotations

import re

from . import specs

#: Fields that read as a size, ordered smallest unit first, for sorting rows.
_CAPACITY = re.compile(r"(?<![\w.])(\d[\d,]*)\s*(GB|TB|MB|mAh)\b", re.IGNORECASE)

_UNIT_SCALE = {"MB": 0.001, "GB": 1.0, "TB": 1024.0, "MAH": 0.001}

#: Reading order when no category is known: the fields people say out loud first.
_CONVENTIONAL_ORDER = ("capacity", "storage", "colour", "color", "size")


def infer_variant(
    title: str | None, category: str | None = None
) -> tuple[str | None, dict[str, str]]:
    """Propose ``(label, attributes)`` for a listing title.

    Returns ``(None, {})`` when nothing recognisable is found -- the caller should then ask
    rather than invent a label. ``category`` overrides detection, for a group whose kind
    somebody has already established.
    """
    if not title:
        return None, {}

    resolved, attributes = specs.read_specs(title, category)
    if not attributes:
        return None, {}

    label = variant_label(attributes, resolved)
    return (label or None), attributes


def infer_variant_from_url(
    url: str | None, category: str | None = None
) -> tuple[str | None, dict[str, str]]:
    """Read a variant out of a URL slug: ".../apple-iphone-17-256gb-black-/p/317396".

    This matters most for the shops that block us. Croma serves nothing we can parse, so the
    listing has no title at all -- but the retailer still wrote the model and colour into the
    path, and reading what they published there is not the same as guessing.

    Weaker evidence than a title, so it is only consulted as a fallback.
    """
    if not url:
        return None, {}
    path = url.split("://", 1)[-1]
    return infer_variant(re.sub(r"[-_/+]+", " ", path), category)


def variant_label(attributes: dict[str, str], category: str | None = None) -> str:
    """Render attributes as a stable label: "256GB / Lavender".

    Only the fields the category says *name* a model are used -- a phone's storage and
    colour, an earbud's colour. RAM is read and displayed but does not go in the label,
    because two phones differing only in RAM are still listed as one model by most shops.

    The order comes from the profile rather than from dict ordering, which matters because
    the label carries a uniqueness constraint: the same attributes must always produce the
    same label.
    """
    if category is None:
        # No category means "use everything, in a stable order" rather than "use a
        # category's naming fields" -- a caller who has not established what the product
        # is should not silently lose its storage size.
        ordered = [attributes[key] for key in _CONVENTIONAL_ORDER if attributes.get(key)]
        ordered += [
            attributes[key] for key in sorted(attributes) if key not in _CONVENTIONAL_ORDER
        ]
        return " / ".join(ordered)

    wanted = specs.label_fields(category)
    ordered = [attributes[name] for name in wanted if attributes.get(name)]
    if not ordered:
        # A category whose naming fields are all absent still deserves a label if anything
        # was read at all -- otherwise a perfectly good reading is thrown away.
        ordered = [attributes[key] for key in sorted(attributes)]
    return " / ".join(ordered)


def sort_position(attributes: dict[str, str]) -> int:
    """Display order: by size ascending, so 128GB precedes 1TB rather than following it.

    Alphabetical ordering puts "1TB" between "128GB" and "256GB", and "20000mAh" before
    "5000mAh", both of which look like bugs to anyone reading the table.
    """
    for value in attributes.values():
        match = _CAPACITY.match(value)
        if match is not None:
            amount = int(match.group(1).replace(",", ""))
            return int(amount * _UNIT_SCALE[match.group(2).upper()])
    return 0


def infer_storage(title: str) -> str | None:
    """The storage capacity, normalised to "256GB". None when unreadable.

    Kept as a named function because the rule it applies is worth testing on its own: each
    "RAM" token claims the single *nearest* capacity, so "8GB RAM, 256GB Storage" yields
    256GB rather than nothing.
    """
    _category, attributes = specs.read_specs(title, "phone")
    return attributes.get("storage")


def infer_colour(title: str) -> str | None:
    """The colour, in title case. None when no known colour appears."""
    _category, attributes = specs.read_specs(title, specs.GENERIC)
    return attributes.get("colour")


# Re-exported so callers that only care about categories need not import both modules.
detect_category = specs.detect_category
render_specs = specs.render_specs
display_fields = specs.display_fields
