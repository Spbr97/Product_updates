"""Reading a product's specifications out of its title, by category.

What separates two models depends entirely on what they are. Phones differ by storage and
colour; earbuds by how long they play and whether they cancel noise; power banks by mAh and
output watts. Reading storage and colour from everything -- which is what this project did
until now -- gives three identical rows for three different earbuds.

So each category is a profile in ``spec_profiles/``: how to recognise it, which fields to
read, which of them name a variant, and which are worth showing in a comparison. Adding a
category is a YAML file, not Python.

The profiles are matched against the title, most specific first, and anything unrecognised
falls back to ``generic`` -- colour only. That fallback is deliberate: pulling every number
out of an unfamiliar title produces confident nonsense, and a wrong specification is worse
than an absent one.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path
from typing import Any

import yaml

from ..core.logging import get_logger

log = get_logger(__name__)

PROFILE_DIR = Path(__file__).parent / "spec_profiles"

GENERIC = "generic"

#: Order profiles are considered in. Only a tie-breaker of last resort now that matching
#: is scored -- generic is excluded because it matches nothing and is the fallback.
_PREFERRED_ORDER = ("phone", "powerbank", "earbuds")

# Capacity, with the unit attached or spaced. Deliberately not matching bare numbers: a
# model number must never be read as a size.
_CAPACITY = re.compile(r"(?<![\w.])(\d{1,4})\s*(GB|TB|MB)\b", re.IGNORECASE)

_UNIT_TO_GB = {"MB": 0.001, "GB": 1.0, "TB": 1024.0}

#: Longest-first so "Space Black" wins over "Black" and "Rose Gold" over "Gold".
_COLOURS: tuple[str, ...] = (
    "Desert Titanium", "Natural Titanium", "Black Titanium", "White Titanium",
    "Blue Titanium", "Silver Shadow", "Space Black", "Space Grey", "Space Gray",
    "Midnight Blue", "Midnight Black", "Pacific Blue", "Sierra Blue", "Alpine Green",
    "Rose Gold", "Starlight", "Ultramarine", "Lavender", "Midnight", "Graphite",
    "Titanium", "Charcoal", "Burgundy", "Platinum", "Turquoise", "Champagne",
    "Sapphire", "Obsidian", "Magenta", "Crimson", "Emerald", "Jetblack", "Icyblue",
    "Lilac", "Violet", "Purple", "Yellow", "Orange", "Silver", "Golden", "Bronze",
    "Copper", "Indigo", "Maroon", "Green", "Black", "White", "Blue", "Beige",
    "Cream", "Coral", "Peach", "Ivory", "Khaki", "Olive", "Sage", "Grey", "Gray",
    "Pink", "Gold", "Teal", "Mint", "Navy", "Sand", "Rose", "Plum", "Cyan", "Red",
)

_COLOUR_PATTERN = re.compile(
    r"\b(" + "|".join(re.escape(colour) for colour in _COLOURS) + r")\b", re.IGNORECASE
)


@dataclass(frozen=True, slots=True)
class SpecField:
    """One readable property of a product."""

    name: str
    kind: str  # capacity | measure | colour | flag
    label: str
    pattern: str | None = None
    unit: str | None = None
    exclude_near: str | None = None
    require_near: str | None = None
    prefer_near: str | None = None


@dataclass(frozen=True, slots=True)
class SpecProfile:
    """How to recognise one kind of product, and what to read from it."""

    category: str
    match: tuple[str, ...] = ()
    fields: tuple[SpecField, ...] = ()
    #: Fields that name a variant. Two listings agreeing on all of these are the same model.
    label: tuple[str, ...] = ()
    #: Fields worth showing beside a price, in this order.
    display: tuple[str, ...] = field(default_factory=tuple)

    def score(self, text: str) -> tuple[int, int]:
        """How strongly this profile claims a title: ``(patterns matched, -earliest hit)``.

        Counting rather than taking the first profile that matches at all. A power bank's
        title lists what it charges -- "Supports Android, Apple, Tablets, Earbuds, Watch" --
        so an incidental "earbuds" made it an earbuds listing, with an mAh capacity that
        no earbuds profile knows how to read. Two hits ("power bank" and "20000mAh") beat
        one, and where the count ties the word appearing earlier in the title wins, because
        a product is usually named before its compatibility list.
        """
        hits = [
            match.start()
            for pattern in self.match
            if (match := re.search(pattern, text, re.IGNORECASE)) is not None
        ]
        if not hits:
            return (0, 0)
        return (len(hits), -min(hits))


def normalise(text: str) -> str:
    """Fold accents and collapse whitespace so matching survives typography."""
    folded = unicodedata.normalize("NFKD", text)
    stripped = "".join(ch for ch in folded if not unicodedata.combining(ch))
    return re.sub(r"\s+", " ", stripped).strip()


@cache
def load_profile(category: str) -> SpecProfile:
    raw: dict[str, Any] = (
        yaml.safe_load((PROFILE_DIR / f"{category}.yaml").read_text(encoding="utf-8")) or {}
    )
    fields = tuple(
        SpecField(
            name=name,
            kind=str(spec.get("kind", "measure")),
            label=str(spec.get("label", name.title())),
            pattern=spec.get("pattern"),
            unit=spec.get("unit"),
            exclude_near=spec.get("exclude_near"),
            require_near=spec.get("require_near"),
            prefer_near=spec.get("prefer_near"),
        )
        for name, spec in (raw.get("fields") or {}).items()
    )
    return SpecProfile(
        category=str(raw.get("category", category)),
        match=tuple(raw.get("match") or ()),
        fields=fields,
        label=tuple(raw.get("label") or ()),
        display=tuple(raw.get("display") or ()),
    )


@cache
def available_profiles() -> tuple[str, ...]:
    if not PROFILE_DIR.is_dir():
        return (GENERIC,)
    found = {path.stem for path in PROFILE_DIR.glob("*.yaml")}
    ordered = [name for name in _PREFERRED_ORDER if name in found]
    ordered += sorted(found - set(ordered) - {GENERIC})
    return tuple(ordered)


def detect_category(title: str | None) -> str:
    """Which kind of product this title describes. ``generic`` when it cannot be told."""
    if not title:
        return GENERIC
    text = normalise(title)
    best_name, best_score = GENERIC, (0, 0)
    for name in available_profiles():
        score = load_profile(name).score(text)
        if score > best_score:
            best_name, best_score = name, score
    return best_name


# --- field readers ----------------------------------------------------------------------


def _nearest_indices(text: str, spans: list[tuple[int, int]], token: str) -> set[int]:
    """Which capacity matches a token claims -- the *single nearest* one per occurrence.

    A window is the obvious approach and it is wrong. In "8GB RAM, 256GB Storage" the word
    RAM sits close to both numbers, so a window discards the storage figure too and the
    title yields no storage at all.
    """
    claimed: set[int] = set()
    for hit in re.finditer(token, text, re.IGNORECASE):
        best: tuple[int, int] | None = None
        for index, (start, end) in enumerate(spans):
            if index in claimed:
                continue
            distance = min(abs(hit.start() - end), abs(start - hit.end()))
            if best is None or distance < best[0]:
                best = (distance, index)
        if best is not None:
            claimed.add(best[1])
    return claimed


def _read_capacity(text: str, spec: SpecField) -> str | None:
    matches = list(_CAPACITY.finditer(text))
    if not matches:
        return None
    spans = [(m.start(), m.end()) for m in matches]

    def rendered(match: re.Match[str]) -> str:
        return f"{int(match.group(1))}{match.group(2).upper()}"

    def size(match: re.Match[str]) -> float:
        return int(match.group(1)) * _UNIT_TO_GB[match.group(2).upper()]

    if spec.require_near:
        wanted = _nearest_indices(text, spans, spec.require_near)
        candidates = [m for i, m in enumerate(matches) if i in wanted]
        return rendered(candidates[0]) if candidates else None

    excluded = _nearest_indices(text, spans, spec.exclude_near) if spec.exclude_near else set()

    # An explicit "ROM"/"Storage" just after a capacity settles it outright.
    if spec.prefer_near:
        for index, match in enumerate(matches):
            if index in excluded:
                continue
            if re.search(spec.prefer_near, text[match.end() : match.end() + 12], re.IGNORECASE):
                return rendered(match)

    candidates = [m for i, m in enumerate(matches) if i not in excluded]
    if not candidates:
        return None
    # Largest wins: where several survive, storage is the bigger figure on every consumer
    # device we are likely to see.
    return rendered(max(candidates, key=size))


def _read_measure(text: str, spec: SpecField) -> str | None:
    if not spec.pattern:
        return None
    match = re.search(spec.pattern, text, re.IGNORECASE)
    if match is None:
        return None
    value = match.group(1).replace(",", "")
    return f"{value}{spec.unit or ''}"


def _read_colour(text: str, _spec: SpecField) -> str | None:
    match = _COLOUR_PATTERN.search(text)
    if match is None:
        return None
    return " ".join(word.capitalize() for word in match.group(1).split())


def _read_flag(text: str, spec: SpecField) -> str | None:
    if not spec.pattern:
        return None
    return spec.label if re.search(spec.pattern, text, re.IGNORECASE) else None


_READERS = {
    "capacity": _read_capacity,
    "measure": _read_measure,
    "colour": _read_colour,
    "flag": _read_flag,
}


def read_specs(title: str | None, category: str | None = None) -> tuple[str, dict[str, str]]:
    """Everything readable from a title. Returns ``(category, attributes)``.

    Anything the profile cannot find is simply absent -- never a placeholder, and never a
    guess. An empty result is a perfectly good answer.
    """
    if not title:
        return category or GENERIC, {}

    text = normalise(title)
    resolved = category or detect_category(title)
    profile = load_profile(resolved)

    attributes: dict[str, str] = {}
    for spec in profile.fields:
        reader = _READERS.get(spec.kind)
        if reader is None:
            log.debug("specs.unknown_kind", kind=spec.kind, field=spec.name)
            continue
        value = reader(text, spec)
        if value:
            attributes[spec.name] = value
    return resolved, attributes


def label_fields(category: str) -> tuple[str, ...]:
    """The fields that name a variant for this category."""
    return load_profile(category).label


def display_fields(category: str) -> tuple[str, ...]:
    """The fields worth showing beside a price, in order."""
    return load_profile(category).display


def render_specs(category: str | None, attributes: dict[str, str]) -> str:
    """The one-line summary shown in a comparison: ``50h · 13mm · ANC``."""
    if not attributes:
        return ""
    wanted = display_fields(category or GENERIC)
    parts = [attributes[name] for name in wanted if attributes.get(name)]
    return " · ".join(parts)
