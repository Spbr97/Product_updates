"""Shared HTTP fetching for store adapters.

One place decides what an HTTP outcome *means*, so every adapter classifies failures the
same way and the tracking engine can act on the classification without site knowledge.

The classification that matters:

* 403 / 429 and CAPTCHA-looking bodies  -> ``BLOCKED``. Recorded, never worked around,
  and never retried immediately -- retrying is precisely what the site is objecting to.
* 404 / 410                             -> ``UNAVAILABLE``. The listing is gone. This is a
  real finding about the product, not a failure.
* 5xx, timeouts, connection resets      -> ``TIMEOUT`` / ``HTTP_ERROR``, both transient.
* other 4xx                             -> ``HTTP_ERROR``, not retried.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import httpx

from ..core.logging import get_logger
from ..domain.enums import Availability, FetchMethod, FetchOutcome
from ..domain.errors import InvalidURLError, UnsafeURLError
from ..domain.models import FetchContext, FetchResult
from ..utils.urls import assert_public_host, host_of, redact_urls

log = get_logger(__name__)

#: Phrases that indicate an anti-bot interstitial rather than a product page. Matched only
#: against the first part of the body, where such pages put their message.
_BLOCK_MARKERS = re.compile(
    r"(captcha|are you a human|are you a robot|unusual traffic|access denied"
    r"|verify you are|bot detection|cf-browser-verification|/errors/validateCaptcha"
    r"|request blocked|pardon our interruption)",
    re.IGNORECASE,
)
_BLOCK_SCAN_BYTES = 20_000


@dataclass(frozen=True, slots=True)
class FetchFailure:
    """A classified HTTP problem, ready to become a FetchResult."""

    outcome: FetchOutcome
    message: str
    http_status: int | None = None


@dataclass(frozen=True, slots=True)
class FetchSuccess:
    """A page body worth parsing."""

    html: str
    url: str
    http_status: int


@dataclass(frozen=True, slots=True)
class FetchBytes:
    """A response body kept as bytes, for content that is not text.

    Exists because ``.xml.gz`` is not text and must never be treated as it. Recovering
    compressed bytes from an already-decoded string -- re-encoding through latin-1 and
    hoping -- is lossy, and it lost: Flipkart's browse sitemaps failed to inflate that way
    while the small uncompressed ones survived, which made the bug look like a Flipkart
    problem rather than ours.
    """

    content: bytes
    url: str
    http_status: int


@dataclass(frozen=True, slots=True)
class _Raw:
    """One successful response, before anything decides whether it is text."""

    content: bytes
    url: str
    http_status: int
    encoding: str | None


def fetch(url: str, ctx: FetchContext) -> FetchSuccess | FetchFailure:
    """Fetch a page over HTTP, returning either the body or a classified failure."""
    raw = _fetch_raw(url, ctx)
    if isinstance(raw, FetchFailure):
        return raw
    body = raw.content.decode(raw.encoding or "utf-8", errors="replace")
    if _looks_blocked(body):
        return FetchFailure(
            FetchOutcome.BLOCKED,
            "page looks like an anti-bot challenge rather than a product page",
            http_status=raw.http_status,
        )
    return FetchSuccess(html=body, url=raw.url, http_status=raw.http_status)


def fetch_bytes(url: str, ctx: FetchContext) -> FetchBytes | FetchFailure:
    """Fetch a body and keep it as bytes.

    Same guards as :func:`fetch` -- the SSRF re-check before connecting and again after
    redirects, the status classification, the size cap. The only difference is that the
    body is not decoded, because for gzip there is nothing to decode it into.
    """
    raw = _fetch_raw(url, ctx)
    if isinstance(raw, FetchFailure):
        return raw
    # An anti-bot page is HTML however it was requested, so the check still applies -- run
    # against a decoded prefix, since binary will simply not match the markers.
    if _looks_blocked(raw.content[:_BLOCK_SCAN_BYTES].decode("utf-8", errors="replace")):
        return FetchFailure(
            FetchOutcome.BLOCKED,
            "response looks like an anti-bot challenge rather than the file requested",
            http_status=raw.http_status,
        )
    return FetchBytes(content=raw.content, url=raw.url, http_status=raw.http_status)


def _fetch_raw(url: str, ctx: FetchContext) -> _Raw | FetchFailure:
    """The whole of the network path, shared by both public entry points.

    ``ctx.verify_public_host`` re-runs the SSRF check immediately before connecting.
    Validation at ``add`` time can be defeated by DNS rebinding, so the guard runs again
    here, on the host we are about to contact -- including after redirects.
    """
    verify_host = ctx.verify_public_host
    headers = {
        "User-Agent": ctx.user_agent,
        "Accept-Language": ctx.accept_language,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }

    try:
        if verify_host:
            assert_public_host(host_of(url))

        with httpx.Client(
            headers=headers,
            timeout=ctx.timeout_seconds,
            follow_redirects=True,
            max_redirects=5,
        ) as client, client.stream("GET", url) as response:
            final_url = str(response.url)
            status = response.status_code

            if verify_host and final_url != url:
                # A redirect can point somewhere internal; re-check the final host.
                assert_public_host(host_of(final_url))

            failure = _classify_status(response)
            if failure is not None:
                return failure

            return _Raw(
                content=_read_capped(response, ctx.max_bytes),
                url=final_url,
                http_status=status,
                encoding=response.encoding,
            )

    except httpx.TimeoutException as exc:
        return FetchFailure(FetchOutcome.TIMEOUT, f"request timed out: {_brief(exc)}")
    except httpx.TooManyRedirects as exc:
        return FetchFailure(FetchOutcome.HTTP_ERROR, f"too many redirects: {_brief(exc)}")
    except httpx.HTTPError as exc:
        return FetchFailure(FetchOutcome.HTTP_ERROR, f"request failed: {_brief(exc)}")
    except UnsafeURLError as exc:
        # Refusing to fetch is a result, not a crash: adapters never raise for this.
        return FetchFailure(FetchOutcome.ERROR, f"refused for safety: {exc}")
    except InvalidURLError as exc:
        # Usually a DNS failure, which can recover, so it is classified as transient.
        return FetchFailure(FetchOutcome.ERROR, str(exc))


def _classify_status(response: httpx.Response) -> FetchFailure | None:
    status = response.status_code

    if status in (401, 403):
        return FetchFailure(
            FetchOutcome.BLOCKED,
            f"store refused the request (HTTP {status}); it may require sign-in or is "
            "blocking automated access",
            http_status=status,
        )
    if status == 429:
        return FetchFailure(
            FetchOutcome.BLOCKED,
            "store rate-limited the request (HTTP 429)",
            http_status=status,
        )
    if status in (404, 410):
        return FetchFailure(
            FetchOutcome.UNAVAILABLE,
            f"listing not found (HTTP {status})",
            http_status=status,
        )
    if status >= 500:
        return FetchFailure(
            FetchOutcome.HTTP_ERROR,
            f"store returned HTTP {status}",
            http_status=status,
        )
    if status >= 400:
        return FetchFailure(
            FetchOutcome.HTTP_ERROR,
            f"unexpected HTTP {status}",
            http_status=status,
        )
    return None


def _read_capped(response: httpx.Response, max_bytes: int) -> bytes:
    """Read at most ``max_bytes``, so a huge or endless response cannot exhaust memory."""
    chunks: list[bytes] = []
    total = 0
    for chunk in response.iter_bytes():
        chunks.append(chunk)
        total += len(chunk)
        if total >= max_bytes:
            log.warning(
                "fetch.truncated", url_host=host_of(str(response.url)), limit_bytes=max_bytes
            )
            break
    return b"".join(chunks)


def _looks_blocked(html: str) -> bool:
    return bool(_BLOCK_MARKERS.search(html[:_BLOCK_SCAN_BYTES]))


def failure_to_result(failure: FetchFailure, method: FetchMethod) -> FetchResult:
    """Turn a transport-level failure into a :class:`FetchResult`.

    Availability stays ``UNKNOWN`` for every failure except a 404/410, which is a positive
    finding that the listing is gone rather than a failure to read it.
    """
    if failure.outcome is FetchOutcome.UNAVAILABLE:
        return FetchResult(
            outcome=FetchOutcome.UNAVAILABLE,
            availability=Availability.UNAVAILABLE,
            fetch_method=method,
            http_status=failure.http_status,
            message=failure.message,
        )
    return FetchResult.failure(
        failure.outcome,
        failure.message,
        fetch_method=method,
        http_status=failure.http_status,
    )


def _brief(exc: Exception) -> str:
    """A short description with the identifying part of any URL removed.

    httpx embeds the request URL in several of its messages, and a URL can carry a token in
    its query string or credentials in its userinfo. This text is stored in
    ``check_executions.error_detail`` and written to logs, so it is reduced to scheme and
    host before it goes anywhere.
    """
    text = redact_urls(str(exc).split("\n", 1)[0])
    return f"{type(exc).__name__}: {text[:120]}" if text else type(exc).__name__
