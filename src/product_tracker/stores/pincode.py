"""Delivery-area awareness for the fetch path.

Indian retailers price and stock per delivery area. "What does this cost?" is a question
with a location in it, and until now this tracker never asked it -- every check took
whatever the shop's default area returned, and recorded it as *the* price. That is not
wrong often enough to notice and not right often enough to trust.

This module is the one place that knows what a given host can do with a PIN code.
Adapters never read ``ctx.delivery_pincode`` themselves; they call :func:`apply` on the
way out and :func:`escalate` on the way back, and the per-host answer lives here.

**What this honestly does today.** Every entry in :data:`RULES` is marked ``needs_js`` or
``location_independent``; not one carries a cookie or a query parameter. That is not an
oversight. Every Indian pincode flow examined is a session handshake -- Amazon's
address-change POST with a CSRF token, Flipkart's location API, BigBasket's
address-bound cookie -- and reproducing one means holding a browser session, which is
the anti-bot line this project does not cross. So :func:`apply` is a true no-op right
now, and all of the module's value is in :func:`escalate`.

:func:`escalate` covers two failures, and the second is the one that made this module
necessary:

* A shop that prices per area returned no price, and a PIN code was configured. Report
  ``NEEDS_LOCATION`` rather than let "no price found" read as a broken selector.
* **A shop that stocks nothing until a delivery area is chosen said "out of stock".**
  Blinkit does this: a cold fetch of a widely-available carton of milk returns valid
  JSON-LD claiming ``OutOfStock``, because nothing is deliverable to an address we never
  gave. Recorded as stock, that is a confident false fact about the product -- the exact
  failure this project exists to refuse, and worse than a missing price because the page
  looks like it answered. Availability is forced back to ``UNKNOWN``.

The ``cookies`` and ``query_param`` fields exist so that a host which *can* be localised
statically becomes one line here, with no change to the fetch path or any adapter.

A host absent from :data:`RULES` means "we do not know", which is the safe default:
nothing is applied and nothing is escalated, exactly as before this module existed.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from ..domain.enums import Availability, FetchOutcome
from ..domain.models import FetchContext, FetchResult
from ..utils.urls import host_of


@dataclass(frozen=True, slots=True)
class PincodeRule:
    """What one host does with a delivery area, and what we can do about it.

    ``needs_js`` and ``location_independent`` are mutually exclusive claims about the
    shop: the first says the price depends on an area we cannot set without a browser
    session, the second says it does not depend on one at all.

    **The two flags are held to different standards of evidence, because they cost
    different things when wrong.**

    ``needs_js`` only ever relabels an extraction that already failed, so being wrong
    costs a misleading diagnosis -- "set a delivery area" when the real answer was "fix
    the selectors". Note that a shop handing us a price on a cold request does *not*
    disprove it: getting *a* price from an unstated area is the exact problem this module
    describes, not evidence against it.

    ``stock_gated_by_area`` discards the shop's own stock claims, so being wrong throws
    away real findings -- quieter than the bug it prevents and just as dishonest. It may
    only be set where that behaviour was *observed*. BigBasket is why this paragraph
    exists: it was marked stock-gated on reasoning alone, and a cold fetch then returned
    a real price and ``unknown`` availability.
    """

    #: Cookie names whose value is the bare PIN code. Empty for every host today.
    cookies: tuple[str, ...] = ()
    #: A query parameter carrying the bare PIN code, if the host accepts one.
    query_param: str | None = None
    #: Prices vary by delivery area, and setting one needs a JavaScript/session
    #: handshake. A price read without it came from somewhere else.
    needs_js: bool = False
    #: Prices are national; a delivery area changes nothing worth reporting.
    location_independent: bool = False
    #: The shop stocks *nothing* until a delivery area is chosen, so with none set its
    #: page says "out of stock" about every product on it. That is a statement about our
    #: missing address, not about the product, and it must never be recorded as stock.
    stock_gated_by_area: bool = False
    #: Why this host is classified the way it is.
    note: str = ""


#: Keyed by registrable domain; matched exactly or as a subdomain, like the catalogue.
RULES: dict[str, PincodeRule] = {
    "amazon.in": PincodeRule(
        needs_js=True,
        note="Delivery area is set by an address-change POST carrying a CSRF token and "
        "bound to the session cookie; there is no standalone cookie or parameter.",
    ),
    "flipkart.com": PincodeRule(
        needs_js=True,
        note="Pincode is set through a location API and held in the session, not in a "
        "cookie we can present on a cold request.",
    ),
    "bigbasket.com": PincodeRule(
        needs_js=True,
        note="Prices vary by serviceable area and the address cookie is issued by the "
        "site against a session, not composed from a PIN code. Deliberately NOT marked "
        "stock_gated_by_area: it looks like Blinkit, but measured 2026-09-03 a cold "
        "fetch returns a real price and availability 'unknown' -- it does not claim "
        "OutOfStock without an address. Discarding its out-of-stock claims would throw "
        "away real findings to solve a problem it does not have.",
    ),
    "blinkit.com": PincodeRule(
        needs_js=True,
        stock_gated_by_area=True,
        note="Quick commerce: nothing at all is stocked or priced until a delivery area "
        "is chosen in an app session. Observed 2026-09-03 -- a cold fetch returns valid "
        "JSON-LD claiming OutOfStock for a product that is in fact widely available.",
    ),
    "croma.com": PincodeRule(
        needs_js=True,
        note="Stock and delivery promise are per area. In practice this entry is inert: "
        "measured 2026-09-03, Croma answers 403 at the edge, so a check never reaches a "
        "page and escalate() is never given anything to relabel. Kept so the reasoning "
        "is on record if the block ever lifts.",
    ),
    "reliancedigital.in": PincodeRule(
        needs_js=True,
        note="Availability and delivery promise are resolved per serviceable area. "
        "Measured 2026-09-03: a cold fetch returns a real price and in_stock, which is "
        "the default area's answer, not a national one -- so this stays needs_js. It "
        "reads cleanly today, so escalate() is rarely reached.",
    ),
    # Two national-pricing shops. This reflects what the catalogue records about them
    # rather than a measurement of their pincode behaviour -- so they are marked as
    # needing no location, not as having one we can set.
    "samsung.com": PincodeRule(
        location_independent=True,
        note="Brand store; the listed price is national.",
    ),
    "sangeethamobiles.com": PincodeRule(
        location_independent=True,
        note="Listed prices are national; delivery area affects shipping, not the price.",
    ),
}


def rule_for(url: str) -> PincodeRule | None:
    """The rule for a URL's host, or ``None`` when the host is not classified."""
    host = host_of(url)
    if not host:
        return None
    for domain, rule in RULES.items():
        if host == domain or host.endswith(f".{domain}"):
            return rule
    return None


def apply(url: str, ctx: FetchContext) -> tuple[str, dict[str, str]]:
    """Localise an outgoing request, returning the URL to fetch and cookies to send.

    A true no-op -- ``(url, {})`` -- when no PIN code is configured, when the host is not
    classified, or when the host has no static mechanism to use. That last case is every
    host today; see the module docstring.
    """
    code = ctx.delivery_pincode
    if not code:
        return url, {}

    rule = rule_for(url)
    if rule is None:
        return url, {}

    cookies = dict.fromkeys(rule.cookies, code)
    if rule.query_param:
        url = _with_param(url, rule.query_param, code)
    return url, cookies


#: Availabilities a stock-gated shop reports about *everything* when no delivery area is
#: set. Read off such a page they are facts about our missing address, not the product.
_UNTRUSTWORTHY_WITHOUT_AREA = (Availability.OUT_OF_STOCK, Availability.UNAVAILABLE)


def escalate(url: str, ctx: FetchContext, result: FetchResult) -> FetchResult:
    """Report what a delivery area we could not set cost us, in the two ways it can.

    A successful price read is never touched, and a block stays a block. This only ever
    turns a weaker answer into a more honest one.

    **Case one: no price.** The shop prices per area, a PIN code was configured, and
    :func:`apply` had nothing to send -- so say the price is missing *for this area*
    rather than leaving "no price found" to read as a broken selector. Requires a
    configured PIN code: with none, nobody asked for a location and the miss is just a
    miss.

    **Case two: an out-of-stock claim from a shop that stocks nothing without an area.**
    This one does *not* require a configured PIN code, because it is the more dangerous
    error and it happens by default. Blinkit's page returns valid structured data saying
    ``OutOfStock`` for every product on it until a delivery address exists; recorded as
    stock, that is a false fact about the product, and it is exactly the failure this
    project refuses to make -- worse than the extraction case, because the page looks
    like it answered. Availability is forced back to ``UNKNOWN``: we do not know.
    """
    rule = rule_for(url)
    if rule is None or rule.location_independent:
        return result
    # A host we actually localised is answering about the right place; its claims stand.
    if rule.cookies or rule.query_param:
        return result

    if rule.stock_gated_by_area and result.availability in _UNTRUSTWORTHY_WITHOUT_AREA:
        return replace(
            result,
            outcome=FetchOutcome.NEEDS_LOCATION,
            availability=Availability.UNKNOWN,
            message=(
                "the page says this is not available, but this shop stocks nothing at all "
                "until a delivery area is set, and none could be -- so this is recorded as "
                "unknown rather than as a product that is out of stock"
            ),
        )

    if result.outcome is not FetchOutcome.PRICE_NOT_FOUND or not ctx.delivery_pincode:
        return result
    if not rule.needs_js:
        return result

    return replace(
        result,
        outcome=FetchOutcome.NEEDS_LOCATION,
        message=(
            f"no price on the page, and this shop prices per delivery area: PIN "
            f"{ctx.delivery_pincode} could not be applied without a browser session, so "
            "no price is recorded rather than one from an unknown area"
        ),
    )


def _with_param(url: str, name: str, value: str) -> str:
    """Set one query parameter, replacing any existing copy of it."""
    parts = urlsplit(url)
    query = [(key, val) for key, val in parse_qsl(parts.query, keep_blank_values=True)
             if key != name]
    query.append((name, value))
    return urlunsplit(parts._replace(query=urlencode(query)))
