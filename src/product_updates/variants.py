from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal
import re

from .models import Offer

COLOURS = {"black", "white", "blue", "green", "red", "yellow", "pink", "purple", "silver", "gold", "sage", "lavender", "grey", "gray", "midnight", "starlight", "orange"}

@dataclass(frozen=True)
class Candidate:
    retailer: str
    description: str
    colours: tuple[str, ...]
    price: Decimal
    available: bool
    urls: tuple[str, ...]

def _parts(title: str) -> tuple[str, tuple[str, ...]]:
    words = re.findall(r"[a-z0-9]+", title.lower())
    colours = tuple(sorted({word.title() for word in words if word in COLOURS}))
    base = " ".join(word for word in words if word not in COLOURS)
    return base, colours

def group_candidates(offers: list[Offer]) -> list[Candidate]:
    groups: dict[tuple, list[Offer]] = defaultdict(list)
    for offer in offers:
        if offer.price is not None:
            base, _ = _parts(offer.title)
            groups[(offer.retailer, base, offer.price, offer.available)].append(offer)
    result = []
    for (retailer, base, price, available), group in groups.items():
        colours = tuple(sorted({colour for offer in group for colour in _parts(offer.title)[1]}))
        result.append(Candidate(retailer, group[0].title, colours, price, available, tuple(offer.url for offer in group)))
    return sorted(result, key=lambda c: (c.retailer, c.price, c.description))
