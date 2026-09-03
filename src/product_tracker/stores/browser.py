"""Playwright rendering for pages that build their content with JavaScript.

Used only as a fallback: a plain HTTP fetch is tried first because it is an order of
magnitude cheaper. Playwright is an optional dependency, so this module must be importable
without it -- the import happens inside the functions.

This renders public pages exactly as a browser would. It does not sign in, supply
credentials, solve challenges, or otherwise defeat access controls.

**Sessions.** Launching Chromium costs about eighteen seconds, and a one-shot ``render``
pays it every single time. Anything that renders several pages in a row -- a search fanning
out across shops, a ``check-all`` sweep -- can open one session and reuse the browser::

    with browser.session():
        render(first, ctx)     # pays the launch
        render(second, ctx)    # does not

``render`` picks the session up from an ambient :class:`~contextvars.ContextVar`, so no
caller and no adapter changes signature, and code that renders a single page is untouched.
A ContextVar is per-thread by construction, which is what makes this safe to add without
touching the scheduler: its jobs run on a thread pool, and Playwright's sync API is not
thread-safe, so a session opened in one thread must never be visible to another.
"""

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass
from types import TracebackType
from typing import Any

from ..core.logging import get_logger
from ..domain.enums import FetchOutcome
from ..domain.errors import InvalidURLError, UnsafeURLError
from ..domain.models import FetchContext
from ..utils.urls import assert_public_host, host_of, redact_urls
from . import pincode
from .http import FetchFailure, FetchSuccess

log = get_logger(__name__)

#: Time to let client-side rendering settle after the DOM is ready, when the caller has not
#: named something better to wait for.
_SETTLE_MS = 1500

#: Longest to wait for a ``wait_for`` selector, regardless of the fetch timeout. Content
#: that has not appeared in twelve seconds is not appearing, and a store whose selector
#: never matches should not spend the whole navigation budget proving it every search.
_WAIT_FOR_CAP_MS = 12_000

#: Message shown when the optional dependency is missing. Public, and compared against by
#: :func:`is_unavailable`, so callers can tell "no browser is installed" apart from "the
#: render failed" -- one is a deployment fact the operator can fix, the other is about the
#: page. Collapsing them would tell someone their shop is broken when their image is lean.
UNAVAILABLE_MESSAGE = (
    "browser rendering unavailable: install the browser extra "
    '(pip install -e ".[browser]" && playwright install chromium) '
    "or set PLAYWRIGHT_ENABLED=false"
)


@dataclass(slots=True)
class _Playwright:
    """The pieces of the optional dependency this module needs."""

    sync_playwright: Any
    error: type[Exception]
    timeout: type[Exception]


def _import_playwright() -> _Playwright | None:
    try:
        from playwright.sync_api import Error as PlaywrightError
        from playwright.sync_api import TimeoutError as PlaywrightTimeout
        from playwright.sync_api import sync_playwright
    except ImportError:
        return None
    return _Playwright(
        sync_playwright=sync_playwright, error=PlaywrightError, timeout=PlaywrightTimeout
    )


@dataclass(slots=True)
class _Active:
    """A live browser, shared by every render inside a session."""

    playwright: _Playwright
    manager: Any
    browser: Any


#: The session in scope, if any. Per-thread, and never inherited across threads.
_SESSION: ContextVar[_Active | None] = ContextVar("product_tracker_browser", default=None)


class BrowserSession:
    """Holds one Chromium open for the duration of a block.

    Entering when Playwright is not installed is deliberately **not** an error: the session
    simply holds nothing, and each ``render`` inside it returns the same install hint it
    would have returned on its own. The caller does not need to care whether the optional
    dependency is present in order to write the ``with`` block.
    """

    def __init__(self, *, headless: bool = True) -> None:
        self.headless = headless
        self._active: _Active | None = None
        self._token: Any = None

    @property
    def available(self) -> bool:
        """Whether a browser is actually open. False when Playwright is missing."""
        return self._active is not None

    def __enter__(self) -> BrowserSession:
        playwright = _import_playwright()
        if playwright is None:
            log.debug("browser.session_unavailable")
            return self

        manager = playwright.sync_playwright().start()
        try:
            browser = manager.chromium.launch(headless=self.headless)
        except Exception:
            manager.stop()
            raise

        self._active = _Active(playwright=playwright, manager=manager, browser=browser)
        self._token = _SESSION.set(self._active)
        log.info("browser.session_started", headless=self.headless)
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if self._token is not None:
            _SESSION.reset(self._token)
            self._token = None
        active, self._active = self._active, None
        if active is None:
            return

        # Close both halves even if the first raises: a leaked Chromium outlives the
        # process that forgot it, and a leaked driver holds a pipe open.
        try:
            active.browser.close()
        except Exception as error:
            log.debug("browser.close_failed", detail=_brief(error))
        finally:
            try:
                active.manager.stop()
            except Exception as error:
                log.debug("browser.stop_failed", detail=_brief(error))
            log.info("browser.session_stopped")


def is_unavailable(result: FetchSuccess | FetchFailure) -> bool:
    """Whether a failure means Playwright is not installed, rather than a render problem."""
    return isinstance(result, FetchFailure) and result.message == UNAVAILABLE_MESSAGE


def session(*, headless: bool = True) -> BrowserSession:
    """Open a browser that every ``render`` in this block will reuse."""
    return BrowserSession(headless=headless)


def render(
    url: str,
    ctx: FetchContext,
    *,
    headless: bool = True,
    wait_for: str | None = None,
) -> FetchSuccess | FetchFailure:
    """Render a page in Chromium and return its HTML, or a classified failure.

    Uses the session in scope when there is one, and otherwise launches a browser for this
    call alone -- which is what every existing caller does and continues to do.

    ``wait_for`` is a CSS selector to wait for before reading the page. It is worth passing
    whenever the caller knows what the content looks like: it returns as soon as that
    content exists instead of sleeping a fixed interval, and it fails honestly when the
    content never arrives rather than handing back a shell.
    """
    # Before anything is launched. Refusing a URL must not cost a browser start, and a
    # host we will not connect to should never reach a page object at all.
    refusal = _refuse_unsafe(url, ctx)
    if refusal is not None:
        return refusal

    active = _SESSION.get()
    if active is not None:
        return _render_with(active, url, ctx, wait_for=wait_for)

    playwright = _import_playwright()
    if playwright is None:
        return FetchFailure(FetchOutcome.ERROR, UNAVAILABLE_MESSAGE)

    try:
        with playwright.sync_playwright() as manager:
            browser = manager.chromium.launch(headless=headless)
            try:
                one_shot = _Active(playwright=playwright, manager=manager, browser=browser)
                return _render_with(one_shot, url, ctx, wait_for=wait_for)
            finally:
                browser.close()
    except playwright.timeout as exc:
        return FetchFailure(FetchOutcome.TIMEOUT, f"browser navigation timed out: {_brief(exc)}")
    except playwright.error as exc:
        return FetchFailure(FetchOutcome.ERROR, f"browser render failed: {_brief(exc)}")


def _render_with(
    active: _Active, url: str, ctx: FetchContext, *, wait_for: str | None
) -> FetchSuccess | FetchFailure:
    """Load one page in an already-running browser."""
    verify_host = ctx.verify_public_host
    playwright = active.playwright
    # Same localisation the plain HTTP path applies, so a rendered check and a fetched
    # one ask the same question. A no-op unless a PIN code is configured and the host
    # has a static way to take one.
    url, cookies = pincode.apply(url, ctx)

    try:
        page = active.browser.new_page(
            locale="en-IN",
            user_agent=ctx.user_agent,
            extra_http_headers={"Accept-Language": ctx.accept_language},
        )
        try:
            if cookies:
                page.context.add_cookies(
                    [
                        {"name": name, "value": value, "url": url}
                        for name, value in cookies.items()
                    ]
                )
            response = page.goto(
                url, wait_until="domcontentloaded", timeout=ctx.timeout_seconds * 1000
            )
            _settle(page, wait_for, ctx)

            status = response.status if response is not None else None
            final_url = page.url
            if verify_host and final_url != url:
                assert_public_host(host_of(final_url))

            html = page.content()
        finally:
            # Pages are closed individually; the browser outlives them in a session.
            page.close()

    except playwright.timeout as exc:
        return FetchFailure(FetchOutcome.TIMEOUT, f"browser navigation timed out: {_brief(exc)}")
    except playwright.error as exc:
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


def _refuse_unsafe(url: str, ctx: FetchContext) -> FetchFailure | None:
    """Classify a URL we will not connect to, before any browser is started."""
    if not ctx.verify_public_host:
        return None
    try:
        assert_public_host(host_of(url))
    except UnsafeURLError as exc:
        return FetchFailure(FetchOutcome.ERROR, f"refused for safety: {exc}")
    except InvalidURLError as exc:
        return FetchFailure(FetchOutcome.ERROR, str(exc))
    return None


def _settle(page: Any, wait_for: str | None, ctx: FetchContext) -> None:
    """Wait for the content to exist, or failing that, for rendering to settle.

    A missing ``wait_for`` selector is not an error here. The page may genuinely not hold
    what we hoped for, and that is for the caller's parser to report -- this returns the
    page as it stands rather than turning a readable-but-different page into a timeout.
    """
    if wait_for:
        timeout = min(ctx.timeout_seconds * 1000, _WAIT_FOR_CAP_MS)
        try:
            page.wait_for_selector(wait_for, timeout=timeout, state="attached")
            return
        except Exception as error:
            # Expected and handled, so it is summarised rather than logged with a
            # traceback: a hundred lines of Playwright stack for a fallback that worked
            # exactly as designed reads like a crash to anyone running the command.
            log.debug("browser.wait_for_missed", selector=wait_for, detail=_brief(error))
    page.wait_for_timeout(_SETTLE_MS)


def _brief(exc: Exception) -> str:
    """First line only, with URLs reduced to their host.

    Playwright errors are long and routinely quote the URL that was being loaded.
    """
    text = redact_urls(str(exc).split(chr(10), 1)[0])
    return f"{type(exc).__name__}: {text[:120]}"
