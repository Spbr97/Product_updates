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

And one thing this must not delegate: **which rules apply to us.**

Flipkart's robots.txt contains eleven separate ``User-agent: *`` groups. RFC 9309 section
2.2.1 says groups with the same user-agent are to be merged, and ``urllib.robotparser``
does not do so consistently across Python versions -- 3.14 merged them and refused
``/search?``, 3.12 did not and permitted it. Byte-identical file, identical user agent,
opposite answers. The deployed image runs 3.12, so the permissive answer was the one that
counted, and the tool was fetching a path Flipkart asks crawlers not to touch -- which is
precisely the failure described above, returning by a side door.

So the groups that apply to us are merged here, into one, before the parser sees them.
Then there is nothing for a parser to disagree about.
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
        parser.parse(rules_for(response.html, ctx.user_agent))
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


def _agent_matches(token: str, user_agent: str) -> bool:
    """Whether a ``User-agent:`` token names us, by the convention robots.txt uses.

    A token matches if it is the wildcard, or if it appears in our user agent string --
    which is how every robots.txt parser reads it, ours included.
    """
    token = token.strip().lower()
    return token == "*" or (bool(token) and token in user_agent.lower())


def rules_for(body: str, user_agent: str) -> list[str]:
    """Every rule that applies to ``user_agent``, merged into one group.

    Two things this gets right that handing the raw file to ``RobotFileParser`` does not:

    * **Repeated groups are merged.** A site may state its rules for ``*`` in eleven
      separate blocks -- Flipkart does -- and all eleven apply. Which of them a parser
      honours otherwise depends on the Python version.
    * **A group naming us wins over the wildcard.** That is what "most specific group"
      means in RFC 9309: if a site has written rules for our agent by name, its ``*``
      rules are not also applied on top.

    Returns lines, ready for ``RobotFileParser.parse``. A file with no group for us comes
    back as an empty group, which permits everything -- the site said nothing about us.
    """
    named: list[str] = []
    wildcard: list[str] = []
    #: Agents of the group being read. Consecutive ``User-agent:`` lines share one group.
    agents: list[str] = []
    starting_group = True

    for raw in body.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or ":" not in line:
            continue
        field, _, value = line.partition(":")
        field = field.strip().lower()
        value = value.strip()

        if field in {"user-agent", "useragent"}:
            if not starting_group:
                agents = []
                starting_group = True
            agents.append(value)
            continue

        if field not in {"allow", "disallow"}:
            # Sitemap, Crawl-delay, Host: not rules about what may be fetched.
            continue

        starting_group = False
        rule = f"{field.capitalize()}: {value}"
        if any(a.strip() != "*" and _agent_matches(a, user_agent) for a in agents):
            named.append(rule)
        elif any(a.strip() == "*" for a in agents):
            wildcard.append(rule)

    return ["User-agent: *", *(named or wildcard)]


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
