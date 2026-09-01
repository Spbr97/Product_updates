"""Playwright rendering for pages that build their content with JavaScript.

Used only as a fallback: a plain HTTP fetch is tried first because it is an order of
magnitude cheaper. Playwright is an optional dependency, so this module must be importable
without it -- the import happens inside the function.

This renders public pages exactly as a browser would. It does not sign in, supply
credentials, solve challenges, or otherwise defeat access controls.
"""

from __future__ import annotations

from ..core.logging import get_logger
from ..domain.enums import FetchOutcome
from ..domain.errors import InvalidURLError, UnsafeURLError
from ..domain.models import FetchContext
from ..utils.urls import assert_public_host, host_of
from .http import FetchFailure, FetchSuccess

log = get_logger(__name__)

#: Time to let client-side rendering settle after the DOM is ready.
_SETTLE_MS = 1500


def render(
    url: str, ctx: FetchContext, *, headless: bool = True
) -> FetchSuccess | FetchFailure:
    """Render a page in Chromium and return its HTML, or a classified failure."""
    verify_host = ctx.verify_public_host
    try:
        from playwright.sync_api import Error as PlaywrightError
        from playwright.sync_api import TimeoutError as PlaywrightTimeout
        from playwright.sync_api import sync_playwright
    except ImportError:
        return FetchFailure(
            FetchOutcome.ERROR,
            "browser rendering unavailable: install the browser extra "
            '(pip install -e ".[browser]" && playwright install chromium) '
            "or set PLAYWRIGHT_ENABLED=false",
        )

    try:
        if verify_host:
            assert_public_host(host_of(url))

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=headless)
            try:
                page = browser.new_page(
                    locale="en-IN",
                    user_agent=ctx.user_agent,
                    extra_http_headers={"Accept-Language": ctx.accept_language},
                )
                response = page.goto(
                    url, wait_until="domcontentloaded", timeout=ctx.timeout_seconds * 1000
                )
                page.wait_for_timeout(_SETTLE_MS)

                status = response.status if response is not None else None
                final_url = page.url
                if verify_host and final_url != url:
                    assert_public_host(host_of(final_url))

                html = page.content()
            finally:
                browser.close()

    except PlaywrightTimeout as exc:
        return FetchFailure(FetchOutcome.TIMEOUT, f"browser navigation timed out: {_brief(exc)}")
    except PlaywrightError as exc:
        return FetchFailure(FetchOutcome.ERROR, f"browser render failed: {_brief(exc)}")
    except UnsafeURLError as exc:
        return FetchFailure(FetchOutcome.ERROR, f"refused for safety: {exc}")
    except InvalidURLError as exc:
        return FetchFailure(FetchOutcome.ERROR, str(exc))

    if status is not None and status >= 400:
        outcome = (
            FetchOutcome.UNAVAILABLE
            if status in (404, 410)
            else FetchOutcome.BLOCKED
            if status in (401, 403, 429)
            else FetchOutcome.HTTP_ERROR
        )
        return FetchFailure(outcome, f"browser received HTTP {status}", http_status=status)

    return FetchSuccess(html=html, url=final_url, http_status=status or 200)


def _brief(exc: Exception) -> str:
    """First line only: Playwright errors are long and can embed the full URL."""
    return f"{type(exc).__name__}: {str(exc).split(chr(10), 1)[0][:120]}"
