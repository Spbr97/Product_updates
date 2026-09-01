"""Reading robots.txt, and doing what it says.

This exists because of something the project got wrong. A Flipkart search was built and
shipped against ``https://www.flipkart.com/search?q=``, and Flipkart's robots.txt contains::

    Disallow: /search?

Nobody checked. The tool spent a day crawling a path the site explicitly asks crawlers not
to touch, and the resulting 403s were read as "rate limiting" rather than as the site
saying no in the way sites are supposed to be able to say it.

So: robots.txt is consulted before a search request, and a disallowed path is not fetched.

Two deliberate choices about scope:

* **Search is checked; a product URL a person handed us is not.** Fetching a page someone
  explicitly asked to track is closer to that person opening it in a browser than to
  crawling, and every retailer here allows their product pages anyway. Discovery -- walking
  a site looking for URLs nobody named -- is the part robots.txt is about.
* **An unreadable robots.txt allows the request.** A 404 means no restrictions, and a site
  whose robots.txt is briefly failing has not thereby forbidden anything. This is logged
  rather than silent, because "we could not read their rules" is worth knowing.
"""

from __future__ import annotations

import time
import urllib.robotparser
from dataclasses import dataclass
from threading import Lock
from urllib.parse import urlsplit, urlunsplit

from ..core.logging import get_logger
from ..domain.models import FetchContext
from .http import FetchSuccess
from .http import fetch as http_fetch

log = get_logger(__name__)

#: How long a fetched robots.txt is trusted. Long enough that a fan-out costs one fetch per
#: host, short enough that a site changing its mind is honoured the same day.
CACHE_TTL_SECONDS = 3600.0


@dataclass(slots=True)
class _Cached:
    parser: urllib.robotparser.RobotFileParser | None
    fetched_at: float
    readable: bool


_CACHE: dict[str, _Cached] = {}
_LOCK = Lock()


def robots_url_for(url: str) -> str:
    parts = urlsplit(url)
    return urlunsplit((parts.scheme or "https", parts.netloc, "/robots.txt", "", ""))


def _load(url: str, ctx: FetchContext) -> _Cached:
    """Fetch and parse a host's robots.txt, or record that it could not be read.

    Uses the project's own HTTP client rather than ``RobotFileParser.read``. That method
    fetches with urllib's default user agent, which several of these retailers refuse --
    and a refusal there makes the parser return "disallowed" for *every* path, including
    ``/``. Diagnosing that from the outside looks exactly like a site banning you.
    """
    target = robots_url_for(url)
    host = urlsplit(target).netloc

    with _LOCK:
        cached = _CACHE.get(host)
        if cached is not None and (time.monotonic() - cached.fetched_at) < CACHE_TTL_SECONDS:
            return cached

    response = http_fetch(target, ctx)
    if isinstance(response, FetchSuccess) and response.http_status == 200:
        parser = urllib.robotparser.RobotFileParser()
        parser.parse(response.html.splitlines())
        entry = _Cached(parser=parser, fetched_at=time.monotonic(), readable=True)
    else:
        # A 404 is the common case and means "no restrictions". Anything else means we
        # could not read their rules, which is not the same as being forbidden.
        status = getattr(response, "http_status", None)
        log.debug("robots.unreadable", url_host=host, http_status=status)
        entry = _Cached(parser=None, fetched_at=time.monotonic(), readable=False)

    with _LOCK:
        _CACHE[host] = entry
    return entry


def is_allowed(url: str, ctx: FetchContext) -> bool:
    """Whether ``ctx``'s user agent may fetch ``url``, according to the site."""
    entry = _load(url, ctx)
    if entry.parser is None:
        return True
    return bool(entry.parser.can_fetch(ctx.user_agent, url))


def reset_cache() -> None:
    """Forget every cached robots.txt. For tests, and for a long-running worker."""
    with _LOCK:
        _CACHE.clear()
