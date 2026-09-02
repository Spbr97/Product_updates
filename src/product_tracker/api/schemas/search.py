"""Request and response models for product search.

A search answer has two halves and both matter. The hits are what was found; the *gaps*
are the shops that could not answer and why. Returning only the hits would let a client
show "3 results" for a query where three retailers were blocked, which reads as "these are
the prices" when it is really "these are the prices we could see".
"""

from __future__ import annotations

from decimal import Decimal

from pydantic import BaseModel, Field

from ...domain.enums import SearchOutcome


class SearchRequest(BaseModel):
    """What to look for, and how hard to look."""

    query: str = Field(
        min_length=1,
        max_length=200,
        description="Product to look for, as a person would type it.",
        examples=["Galaxy S25"],
    )
    stores: list[str] | None = Field(
        default=None,
        max_length=50,
        description="Restrict to these store slugs. Omit to search every searchable store.",
    )
    limit_per_store: int = Field(
        default=8, ge=1, le=25, description="Candidates to keep from each store."
    )
    allow_browser: bool = Field(
        default=False,
        description=(
            "Render JavaScript-only shops when the quick pass finds no exact match. "
            "Off by default: rendering starts a browser and can push a single request "
            "past a minute, which is longer than most proxies and impatient users allow."
        ),
    )


class SearchHitResponse(BaseModel):
    """One candidate. A suggestion, never a decision.

    ``qualifiers`` names model words present in the title but absent from the query --
    "FE", "Pro", "Plus". They are what separates a Galaxy S25 from a Galaxy S25 FE, two
    phones tens of thousands of rupees apart that both match a search for "Galaxy S25".
    A client that ignores them will happily track the wrong phone.
    """

    url: str
    title: str
    store: str = Field(description="Display name of the retailer.")
    store_slug: str
    price: Decimal | None = Field(
        default=None, description="Null for catalogue hits, which carry no price."
    )
    currency: str | None = None
    image_url: str | None = None
    score: float = Field(description="0.0-1.0. How much of the query the title matched.")
    qualifiers: list[str] = Field(
        default_factory=list, description="Extra model words in the title. Non-empty means "
        "this is a different model from the one asked for."
    )
    is_exact: bool = Field(description="Every query word present, and no extra qualifier.")
    from_sitemap: bool = Field(
        description=(
            "Found in the shop's published catalogue rather than its search. Such a hit "
            "has no price, and its title was derived from the URL rather than published "
            "by the retailer -- do not show it as the shop's own wording."
        )
    )


class StoreGapResponse(BaseModel):
    """A store that did not contribute results, and why.

    ``outcome`` distinguishes "the shop genuinely has nothing" (``no_results``) from "the
    shop would not talk to us" (``blocked``). Collapsing the two would report a product as
    unavailable at a retailer that simply refused the search.
    """

    store: str
    store_slug: str
    outcome: SearchOutcome
    message: str | None = None
    http_status: int | None = None


class SearchResponse(BaseModel):
    query: str
    hits: list[SearchHitResponse] = Field(description="Best match first, across all stores.")
    exact_count: int = Field(description="How many hits matched the query exactly.")
    searched: list[str] = Field(description="Store slugs this search actually queried.")
    gaps: list[StoreGapResponse] = Field(
        default_factory=list, description="Queried stores that returned nothing usable."
    )
    skipped: list[str] = Field(
        default_factory=list,
        description="Catalogued stores with no search description yet. Never searched.",
    )
