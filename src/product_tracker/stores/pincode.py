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
now, and the value of the module is :func:`escalate`: with a PIN code configured, a shop
that prices per area and could not be localised reports ``needs_location`` instead of
letting a price from an unknown area pass as an answer.

The ``cookies`` and ``query_param`` fields exist so that a host which *can* be localised
statically becomes one line here, with no change to the fetch path or any adapter.

A host absent from :data:`RULES` means "we do not know", which is the safe default:
nothing is applied and nothing is escalated, exactly as before this module existed.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from ..domain.enums import FetchOutcome
from ..domain.models import FetchContext, FetchResult
from ..utils.urls import host_of


@dataclass(frozen=True, slots=True)
class PincodeRule:
    """What one host does with a delivery area, and what we can do about it.

    ``needs_js`` and ``location_independent`` are mutually exclusive claims about the
    shop: the first says the price depends on an area we cannot set without a browser
    session, the second says it does not depend on one at all.
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
        note="Groceries are the most area-dependent catalogue of all; the address cookie "
        "is issued by the site against a session, not composed from a PIN code.",
    ),
    "blinkit.com": PincodeRule(
        needs_js=True,
        note="Quick commerce: nothing at all is priced until a delivery area is chosen in "
        "an app session. Checks are expected to fail here regardless.",
    ),
    "croma.com": PincodeRule(
        needs_js=True,
        note="Stock and delivery promise are per area. Croma blocks automated access at "
        "the edge, so a check rarely gets far enough for this to be the reason.",
    ),
    "reliancedigital.in": PincodeRule(
        needs_js=True,
        note="Availability is resolved per serviceable area after the page loads.",
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


def escalate(url: str, ctx: FetchContext, result: FetchResult) -> FetchResult:
    """Relabel "no price found" as "no price *for this area*", where that is the truth.

    Only ever narrows a ``PRICE_NOT_FOUND``. A successful read is never touched, and
    availability is carried through untouched -- a page we could not price is not a page
    that told us the product is gone.

    The conditions are deliberately all of: a PIN code is configured (otherwise the user
    never asked for a location and the miss is just a miss), the host is classified, it
    prices per area, and :func:`apply` had nothing to send. If a static mechanism ever
    lands for a host, its misses stop being about location and this stops firing for it.
    """
    if result.outcome is not FetchOutcome.PRICE_NOT_FOUND:
        return result
    if not ctx.delivery_pincode:
        return result

    rule = rule_for(url)
    if rule is None or rule.location_independent or not rule.needs_js:
        return result
    if rule.cookies or rule.query_param:
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
