"""URL validation, SSRF protection, and canonicalisation.

Two jobs, both security- and correctness-relevant:

* **Validation** decides whether we are willing to fetch a URL at all. The application
  fetches arbitrary user-supplied URLs, which is the textbook SSRF setup, so anything
  resolving into a private range is refused by default.
* **Canonicalisation** produces the key that duplicate detection uses. The same listing
  shared from two places differs only in tracking parameters; normalising them away means
  we track it once.

No dependency on application settings: callers pass the policy in. That keeps this module
trivially testable and usable from anywhere.
"""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from ..domain.errors import InvalidURLError, UnsafeURLError

DEFAULT_SCHEMES: tuple[str, ...] = ("https", "http")
DEFAULT_MAX_LENGTH = 2048

#: Query parameters that identify a referrer or campaign rather than a product. Removing
#: them is what makes two shares of the same listing compare equal.
#:
#: Kept deliberately conservative: a parameter is only listed once it is known to be
#: non-identifying. Flipkart's ``pid`` and Amazon's ``th``/``psc``, for instance, do change
#: which item you get, so they must survive canonicalisation.
TRACKING_PARAMS: frozenset[str] = frozenset(
    {
        # Generic campaign/click identifiers.
        "gclid", "fbclid", "msclkid", "dclid", "igshid", "mc_cid", "mc_eid",
        "yclid", "ttclid", "twclid", "wbraid", "gbraid", "_ga", "_gl",
        # Affiliate.
        "tag", "affid", "affextparam1", "affextparam2", "ascsubtag", "linkcode",
        # Flipkart listing/impression tracking. NOTE: `pid` is NOT here -- it selects
        # the variant and is part of the product's identity.
        "lid", "marketplace", "srno", "otracker", "otracker1", "fm", "iid",
        "ppt", "ppn", "ssid", "qh", "_refid", "cmpid", "spotlighttagid", "store",
        # Amazon / general referrer breadcrumbs.
        "ref_", "refid", "pf_rd_p", "pf_rd_r", "pf_rd_s", "pf_rd_t", "pf_rd_i",
        "pd_rd_w", "pd_rd_r", "pd_rd_wg", "content-id", "smid",
    }
)

#: Any parameter starting with one of these is treated as tracking.
TRACKING_PREFIXES: tuple[str, ...] = ("utm_",)


def _is_tracking_param(name: str) -> bool:
    lowered = name.lower()
    return lowered in TRACKING_PARAMS or lowered.startswith(TRACKING_PREFIXES)


def canonicalize_url(url: str) -> str:
    """Return the normalised form used for duplicate detection.

    Lowercases scheme and host, drops the default port, removes the fragment, strips
    tracking parameters, sorts what remains, and removes a trailing slash. The path's case
    is preserved -- paths are case-sensitive on most servers, unlike hostnames.
    """
    parts = urlsplit(url.strip())

    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower()
    if not host:
        return url.strip()

    netloc = host
    if parts.port and not _is_default_port(scheme, parts.port):
        netloc = f"{host}:{parts.port}"

    path = parts.path
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/")

    pairs = parse_qsl(parts.query, keep_blank_values=True)
    kept = [(k, v) for k, v in pairs if not _is_tracking_param(k)]
    query = urlencode(sorted(kept))

    return urlunsplit((scheme, netloc, path, query, ""))


def _is_default_port(scheme: str, port: int) -> bool:
    return (scheme == "https" and port == 443) or (scheme == "http" and port == 80)


def validate_url(
    url: str,
    *,
    allowed_schemes: tuple[str, ...] = DEFAULT_SCHEMES,
    block_private: bool = True,
    max_length: int = DEFAULT_MAX_LENGTH,
) -> str:
    """Validate a user-supplied URL, returning it stripped of surrounding whitespace.

    Raises :class:`InvalidURLError` for a malformed or disallowed URL and
    :class:`UnsafeURLError` when the host resolves into a private range.
    """
    candidate = url.strip()

    if not candidate:
        raise InvalidURLError("URL must not be empty")
    if len(candidate) > max_length:
        raise InvalidURLError(f"URL exceeds {max_length} characters")

    try:
        parts = urlsplit(candidate)
    except ValueError as exc:
        raise InvalidURLError(f"malformed URL: {exc}") from exc

    scheme = parts.scheme.lower()
    if not scheme:
        raise InvalidURLError("URL must include a scheme (https:// or http://)")
    if scheme not in allowed_schemes:
        allowed = ", ".join(allowed_schemes)
        raise InvalidURLError(f"scheme {scheme!r} is not allowed (allowed: {allowed})")

    # Credentials in a URL would be logged and stored; refuse rather than sanitise.
    if parts.username or parts.password:
        raise InvalidURLError("URL must not embed credentials")

    try:
        host = parts.hostname
    except ValueError as exc:
        raise InvalidURLError(f"malformed host: {exc}") from exc
    if not host:
        raise InvalidURLError("URL must include a host")

    if block_private:
        assert_public_host(host)

    return candidate


def assert_public_host(host: str) -> None:
    """Raise :class:`UnsafeURLError` unless every address ``host`` resolves to is public.

    All resolved addresses are checked, not just the first: a name that returns both a
    public and a private address must not slip through.

    This is a time-of-check guard. It does not by itself defeat DNS rebinding, where a name
    resolves publicly here and privately at fetch time; the same check therefore runs again
    immediately before each fetch.
    """
    for address in resolve_host(host):
        if not _is_public(address):
            raise UnsafeURLError(
                f"{host} resolves to non-public address {address}; refusing to fetch"
            )


def resolve_host(host: str) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    """Resolve a hostname (or parse a literal IP) to a list of addresses."""
    literal = _parse_ip_literal(host)
    if literal is not None:
        return [literal]

    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise InvalidURLError(f"cannot resolve host {host!r}: {exc.strerror or exc}") from exc

    addresses = []
    for info in infos:
        address = _parse_ip_literal(str(info[4][0]))
        if address is not None:
            addresses.append(address)
    if not addresses:
        raise InvalidURLError(f"cannot resolve host {host!r}")
    return addresses


def _parse_ip_literal(value: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    # urlsplit keeps IPv6 literals in brackets; strip them, and drop any zone index.
    stripped = value.strip("[]").split("%", 1)[0]
    try:
        return ipaddress.ip_address(stripped)
    except ValueError:
        return None


def _is_public(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """True only for globally routable addresses.

    ``is_global`` alone is not enough: it does not exclude every range we care about, and
    IPv4-mapped IPv6 addresses (``::ffff:127.0.0.1``) must be unwrapped first or a loopback
    address passes as global.
    """
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped

    return not (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or address.is_reserved
        or address.is_unspecified
    ) and address.is_global


def host_of(url: str) -> str:
    """The lowercase hostname, or an empty string. Safe to put in a log line."""
    try:
        return (urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""
