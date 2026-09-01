"""Working out which model and colour a listing is, from its title.

Stores do not publish a variant identifier we can rely on, so the title is what we have:

    "Apple iPhone 17 (256 GB) - Lavender"
    "Apple iPhone 17 256GB Lavender"
    "APPLE iPhone 17 (Lavender, 256 GB)"

All three are one variant. Reading them as three would split a model across shops and make
the comparison grid useless, which is the whole reason the variant table exists.

This is a *suggestion engine*, deliberately. It proposes a label; a human confirms or
overrides it. Inference that silently mis-groups two models is worse than no inference,
so everything here is conservative: anything it cannot read with confidence it leaves out
rather than guessing, and an empty result is a perfectly good answer.
"""

from __future__ import annotations

import re
import unicodedata

# Capacity, with the unit attached or spaced. Deliberately not matching bare numbers.
_CAPACITY = re.compile(r"(?<![\w.])(\d{1,4})\s*(GB|TB|MB)\b", re.IGNORECASE)

# "8GB RAM", "RAM 8 GB" -- the capacity that is memory, not storage.
_RAM_TOKEN = re.compile(r"\bRAM\b", re.IGNORECASE)

# "256GB ROM", "256 GB Storage", "512GB SSD" -- an explicit statement that it *is* storage.
_STORAGE_TOKEN = re.compile(r"\b(ROM|Storage|SSD|HDD|Internal)\b", re.IGNORECASE)

#: Longest-first so "Space Black" wins over "Black" and "Rose Gold" over "Gold".
_COLOURS: tuple[str, ...] = (
    "Desert Titanium", "Natural Titanium", "Black Titanium", "White Titanium",
    "Blue Titanium", "Silver Shadow", "Space Black", "Space Grey", "Space Gray",
    "Midnight Blue",
    "Midnight Black", "Pacific Blue", "Sierra Blue", "Alpine Green", "Rose Gold",
    "Starlight", "Ultramarine", "Lavender", "Midnight", "Graphite", "Titanium",
    "Charcoal", "Burgundy", "Platinum", "Turquoise", "Champagne", "Sapphire",
    "Obsidian", "Magenta", "Crimson", "Emerald", "Lilac", "Violet", "Purple",
    "Yellow", "Orange", "Silver", "Golden", "Bronze", "Copper", "Indigo",
    "Maroon", "Green", "Black", "White", "Blue", "Beige", "Cream", "Coral",
    "Peach", "Ivory", "Khaki", "Olive", "Sage", "Grey", "Gray", "Pink", "Gold",
    "Teal", "Mint", "Navy", "Sand", "Rose", "Plum", "Cyan", "Red",
)

_COLOUR_PATTERN = re.compile(
    r"\b(" + "|".join(re.escape(colour) for colour in _COLOURS) + r")\b", re.IGNORECASE
)

_UNIT_TO_GB = {"MB": 0.001, "GB": 1.0, "TB": 1024.0}

_ORDERED_KEYS = ("storage", "colour", "color", "size")


def _normalise(text: str) -> str:
    """Fold accents and collapse whitespace so matching is not defeated by typography."""
    folded = unicodedata.normalize("NFKD", text)
    stripped = "".join(ch for ch in folded if not unicodedata.combining(ch))
    return re.sub(r"\s+", " ", stripped).strip()


def _ram_indices(text: str, spans: list[tuple[int, int]]) -> set[int]:
    """Which capacity matches are memory rather than storage.

    Each occurrence of "RAM" claims the *single nearest* capacity, rather than every
    capacity within some window. A window is the obvious approach and it is wrong: in
    "8GB RAM, 256GB Storage" the word RAM sits close to both numbers, so a window discards
    the storage figure too and the title yields no storage at all.
    """
    claimed: set[int] = set()
    for token in _RAM_TOKEN.finditer(text):
        best: tuple[int, int] | None = None
        for index, (start, end) in enumerate(spans):
            if index in claimed:
                continue
            distance = min(abs(token.start() - end), abs(start - token.end()))
            if best is None or distance < best[0]:
                best = (distance, index)
        if best is not None:
            claimed.add(best[1])
    return claimed


def infer_storage(title: str) -> str | None:
    """The storage capacity, normalised to "256GB". None when unreadable."""
    text = _normalise(title)
    matches = list(_CAPACITY.finditer(text))
    if not matches:
        return None

    spans = [(m.start(), m.end()) for m in matches]
    ram = _ram_indices(text, spans)

    def rendered(match: re.Match[str]) -> str:
        return f"{int(match.group(1))}{match.group(2).upper()}"

    def size_gb(match: re.Match[str]) -> float:
        return int(match.group(1)) * _UNIT_TO_GB[match.group(2).upper()]

    # An explicit "ROM"/"Storage"/"SSD" just after a capacity settles it outright.
    for index, match in enumerate(matches):
        if index in ram:
            continue
        if _STORAGE_TOKEN.search(text[match.end() : match.end() + 12]):
            return rendered(match)

    candidates = [m for i, m in enumerate(matches) if i not in ram]
    if not candidates:
        return None
    # Largest wins: where several survive, storage is the bigger figure on every consumer
    # device we are likely to see.
    return rendered(max(candidates, key=size_gb))


def infer_colour(title: str) -> str | None:
    """The colour, in title case. None when no known colour appears."""
    match = _COLOUR_PATTERN.search(_normalise(title))
    if match is None:
        return None
    return " ".join(word.capitalize() for word in match.group(1).split())


def infer_variant(title: str | None) -> tuple[str | None, dict[str, str]]:
    """Propose ``(label, attributes)`` for a listing title.

    Returns ``(None, {})`` when nothing recognisable is found -- the caller should then ask
    rather than invent a label.
    """
    if not title:
        return None, {}

    attributes: dict[str, str] = {}
    if storage := infer_storage(title):
        attributes["storage"] = storage
    if colour := infer_colour(title):
        attributes["colour"] = colour

    if not attributes:
        return None, {}
    return variant_label(attributes), attributes


def variant_label(attributes: dict[str, str]) -> str:
    """Render attributes as a stable label: "256GB / Lavender".

    Storage first, then colour, then anything else alphabetically -- so the same attributes
    always produce the same label regardless of dict ordering, which matters because the
    label carries a uniqueness constraint.
    """
    ordered: list[str] = [str(attributes[k]) for k in _ORDERED_KEYS if attributes.get(k)]
    ordered += [str(attributes[k]) for k in sorted(attributes) if k not in _ORDERED_KEYS]
    return " / ".join(ordered)


def sort_position(attributes: dict[str, str]) -> int:
    """Display order: by storage ascending, so 128GB precedes 1TB rather than following it.

    Alphabetical ordering puts "1TB" between "128GB" and "256GB", which looks like a bug to
    anyone reading the table.
    """
    storage = attributes.get("storage")
    if not storage:
        return 0
    match = _CAPACITY.match(storage)
    if match is None:
        return 0
    return int(int(match.group(1)) * _UNIT_TO_GB[match.group(2).upper()])


def infer_variant_from_url(url: str | None) -> tuple[str | None, dict[str, str]]:
    """Read a variant out of a URL slug: ".../apple-iphone-17-256gb-black-/p/317396".

    This matters most for the shops that block us. Croma serves nothing we can parse, so
    the listing has no title at all -- but the retailer still wrote the model and colour
    into the path, and reading what they published there is not the same as guessing.

    Weaker evidence than a title, so it is only consulted as a fallback. Numeric path
    segments cannot be mistaken for capacities: a capacity requires its unit.
    """
    if not url:
        return None, {}
    path = url.split("://", 1)[-1]
    # Hyphens and slashes are word separators in a slug; underscores too.
    return infer_variant(re.sub(r"[-_/+]+", " ", path))
