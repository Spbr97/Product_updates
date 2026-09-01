"""The Playwright fallback.

Playwright is an optional dependency and launching a real browser in tests would be slow
and flaky, so ``playwright.sync_api`` is replaced with a stub. What is under test is our
handling -- classification, host verification, cleanup -- not Chromium.
"""

from __future__ import annotations

import sys
import types
from collections.abc import Iterator

import pytest

from product_tracker.domain.enums import FetchOutcome
from product_tracker.domain.models import FetchContext
from product_tracker.stores import browser
from product_tracker.stores.http import FetchFailure, FetchSuccess

CTX = FetchContext(timeout_seconds=5, verify_public_host=False)
PAGE_HTML = "<html><body><h1>Rendered</h1></body></html>"


class FakeTimeoutError(Exception):
    """Stands in for playwright's TimeoutError."""


class FakePlaywrightError(Exception):
    """Stands in for playwright's Error."""


class FakeResponse:
    def __init__(self, status: int | None) -> None:
        self.status = status


class FakePage:
    def __init__(self, owner: FakeBrowser) -> None:
        self.owner = owner
        self.url = owner.final_url

    def goto(self, url: str, **_kwargs: object) -> FakeResponse | None:
        self.owner.visited.append(url)
        if self.owner.raise_on_goto is not None:
            raise self.owner.raise_on_goto
        return FakeResponse(self.owner.status)

    def wait_for_timeout(self, _ms: int) -> None:
        self.owner.owner.settled += 1

    def wait_for_selector(self, selector: str, **_kwargs: object) -> object:
        """Stands in for waiting on a results selector."""
        chromium = self.owner.owner
        chromium.waited_for.append(selector)
        if selector in chromium.missing_selectors:
            raise FakeTimeoutError(f"selector not found: {selector}")
        return object()

    def close(self) -> None:
        self.owner.owner.pages_closed += 1

    def content(self) -> str:
        return self.owner.html


class FakeBrowser:
    def __init__(self, owner: FakeChromium) -> None:
        self.owner = owner
        self.pages_opened = 0
        self.html = owner.html
        self.status = owner.status
        self.final_url = owner.final_url
        self.visited = owner.visited
        self.raise_on_goto = owner.raise_on_goto

    def new_page(self, **_kwargs: object) -> FakePage:
        self.pages_opened += 1
        self.owner.pages_opened += 1
        return FakePage(self)

    def close(self) -> None:
        self.owner.closed = True


class FakeChromium:
    def __init__(self, parent: FakePlaywright) -> None:
        self.parent = parent
        self.html = parent.html
        self.status = parent.status
        self.final_url = parent.final_url
        self.visited = parent.visited
        self.raise_on_goto = parent.raise_on_goto
        self.closed = False
        self.launches = 0
        self.pages_opened = 0
        self.pages_closed = 0
        self.settled = 0
        self.waited_for: list[str] = []
        self.missing_selectors: frozenset[str] = parent.missing_selectors

    def launch(self, **_kwargs: object) -> FakeBrowser:
        self.launches += 1
        browser_instance = FakeBrowser(self)
        self.parent.browser = browser_instance
        return browser_instance


class FakePlaywright:
    def __init__(
        self,
        *,
        html: str = PAGE_HTML,
        status: int | None = 200,
        final_url: str = "https://shop.test/p/1",
        raise_on_goto: Exception | None = None,
        missing_selectors: frozenset[str] = frozenset(),
    ) -> None:
        self.html = html
        self.status = status
        self.final_url = final_url
        self.raise_on_goto = raise_on_goto
        self.missing_selectors = missing_selectors
        self.visited: list[str] = []
        self.chromium = FakeChromium(self)
        self.browser: FakeBrowser | None = None
        self.stopped = False

    def __enter__(self) -> FakePlaywright:
        return self

    def __exit__(self, *_args: object) -> None: ...

    # A session drives the manager explicitly rather than through ``with``.
    def start(self) -> FakePlaywright:
        return self

    def stop(self) -> None:
        self.stopped = True

    @property
    def closed(self) -> bool:
        return self.chromium.closed


class PlaywrightStub:
    """Records the browsers a test caused to be launched, and configures the next one."""

    def __init__(self) -> None:
        self.config: dict[str, object] = {}
        self.instances: list[FakePlaywright] = []

    def __len__(self) -> int:
        return len(self.instances)

    def __getitem__(self, index: int) -> FakePlaywright:
        return self.instances[index]

    def launched(self) -> bool:
        return bool(self.instances)


@pytest.fixture
def playwright_stub(monkeypatch: pytest.MonkeyPatch) -> Iterator[PlaywrightStub]:
    """Replace ``playwright.sync_api`` with a stub for the duration of a test."""
    stub = PlaywrightStub()

    def sync_playwright() -> FakePlaywright:
        instance = FakePlaywright(**stub.config)  # type: ignore[arg-type]
        stub.instances.append(instance)
        return instance

    module = types.ModuleType("playwright.sync_api")
    module.sync_playwright = sync_playwright  # type: ignore[attr-defined]
    module.TimeoutError = FakeTimeoutError  # type: ignore[attr-defined]
    module.Error = FakePlaywrightError  # type: ignore[attr-defined]

    package = types.ModuleType("playwright")
    package.sync_api = module  # type: ignore[attr-defined]

    monkeypatch.setitem(sys.modules, "playwright", package)
    monkeypatch.setitem(sys.modules, "playwright.sync_api", module)

    yield stub


class TestMissingDependency:
    def test_reports_how_to_install_it(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The optional extra must fail with instructions, not an ImportError."""
        monkeypatch.setitem(sys.modules, "playwright.sync_api", None)

        result = browser.render("https://shop.test/p/1", CTX)

        assert isinstance(result, FetchFailure)
        assert result.outcome is FetchOutcome.ERROR
        assert "browser extra" in result.message


class TestSuccessfulRender:
    def test_returns_the_rendered_html(
        self, playwright_stub: PlaywrightStub
    ) -> None:
        result = browser.render("https://shop.test/p/1", CTX)

        assert isinstance(result, FetchSuccess)
        assert result.html == PAGE_HTML
        assert result.http_status == 200

    def test_closes_the_browser(self, playwright_stub: PlaywrightStub) -> None:
        """A leaked Chromium process per check would exhaust the machine."""
        browser.render("https://shop.test/p/1", CTX)

        assert playwright_stub[0].closed is True

    def test_navigates_to_the_requested_url(
        self, playwright_stub: PlaywrightStub
    ) -> None:
        browser.render("https://shop.test/p/1", CTX)

        assert playwright_stub[0].visited == ["https://shop.test/p/1"]


class TestFailureClassification:
    def test_timeout(self, playwright_stub: PlaywrightStub) -> None:
        playwright_stub.config["raise_on_goto"] = FakeTimeoutError("navigation timed out")

        result = browser.render("https://shop.test/p/1", CTX)

        assert isinstance(result, FetchFailure)
        assert result.outcome is FetchOutcome.TIMEOUT

    def test_generic_playwright_error(
        self, playwright_stub: PlaywrightStub
    ) -> None:
        playwright_stub.config["raise_on_goto"] = FakePlaywrightError("crashed")

        result = browser.render("https://shop.test/p/1", CTX)

        assert isinstance(result, FetchFailure)
        assert result.outcome is FetchOutcome.ERROR

    def test_the_browser_is_closed_even_on_failure(
        self, playwright_stub: PlaywrightStub
    ) -> None:
        playwright_stub.config["raise_on_goto"] = FakePlaywrightError("crashed")

        browser.render("https://shop.test/p/1", CTX)

        assert playwright_stub[0].closed is True

    @pytest.mark.parametrize(
        ("status", "expected"),
        [
            (404, FetchOutcome.UNAVAILABLE),
            (410, FetchOutcome.UNAVAILABLE),
            (403, FetchOutcome.BLOCKED),
            (429, FetchOutcome.BLOCKED),
            (500, FetchOutcome.HTTP_ERROR),
        ],
    )
    def test_http_statuses_map_the_same_way_as_a_plain_fetch(
        self, playwright_stub: PlaywrightStub, status: int, expected: FetchOutcome
    ) -> None:
        playwright_stub.config["status"] = status

        result = browser.render("https://shop.test/p/1", CTX)

        assert isinstance(result, FetchFailure)
        assert result.outcome is expected

    def test_a_missing_status_is_treated_as_success(
        self, playwright_stub: PlaywrightStub
    ) -> None:
        """A data: or about: navigation returns no response object."""
        playwright_stub.config["status"] = None

        result = browser.render("https://shop.test/p/1", CTX)

        assert isinstance(result, FetchSuccess)
        assert result.http_status == 200

    def test_error_messages_do_not_leak_the_url(
        self, playwright_stub: PlaywrightStub
    ) -> None:
        playwright_stub.config["raise_on_goto"] = FakePlaywrightError(
            "failed navigating to https://shop.test/p/1?token=SECRET"
        )

        result = browser.render("https://shop.test/p/1?token=SECRET", CTX)

        assert isinstance(result, FetchFailure)
        assert "SECRET" not in result.message


class TestHostVerification:
    def test_refuses_a_private_host(self, playwright_stub: PlaywrightStub) -> None:
        """The SSRF guard applies to the browser path too, not just plain HTTP."""
        result = browser.render(
            "http://127.0.0.1/admin", FetchContext(verify_public_host=True)
        )

        assert isinstance(result, FetchFailure)
        assert result.outcome is FetchOutcome.ERROR
        assert "refused for safety" in result.message

    def test_no_browser_is_launched_when_refused(
        self, playwright_stub: PlaywrightStub
    ) -> None:
        browser.render("http://127.0.0.1/admin", FetchContext(verify_public_host=True))

        assert not playwright_stub.launched()


class TestSessionReuse:
    """One Chromium for many renders.

    This is the whole reason sessions exist: a launch costs about eighteen seconds, and a
    fan-out across four shops paid it four times.
    """

    def test_one_launch_serves_every_render(self, playwright_stub: PlaywrightStub) -> None:
        with browser.session():
            for index in range(3):
                assert isinstance(browser.render(f"https://shop.test/p/{index}", CTX), FetchSuccess)

        assert len(playwright_stub) == 1
        assert playwright_stub[0].chromium.launches == 1

    def test_each_render_gets_its_own_page(self, playwright_stub: PlaywrightStub) -> None:
        """Pages are per-render; the browser outlives them."""
        with browser.session():
            for index in range(3):
                browser.render(f"https://shop.test/p/{index}", CTX)
            chromium = playwright_stub[0].chromium
            assert chromium.pages_opened == 3
            # Closed as they go, not accumulated until the session ends.
            assert chromium.pages_closed == 3

    def test_the_browser_is_closed_when_the_session_ends(
        self, playwright_stub: PlaywrightStub
    ) -> None:
        with browser.session():
            browser.render("https://shop.test/p/1", CTX)
        assert playwright_stub[0].closed
        assert playwright_stub[0].stopped

    def test_the_browser_is_closed_even_when_the_block_raises(
        self, playwright_stub: PlaywrightStub
    ) -> None:
        """A leaked Chromium outlives the process that forgot it."""
        with pytest.raises(RuntimeError), browser.session():
            browser.render("https://shop.test/p/1", CTX)
            raise RuntimeError("something went wrong mid-fan-out")

        assert playwright_stub[0].closed
        assert playwright_stub[0].stopped

    def test_a_page_is_closed_even_when_navigation_fails(
        self, playwright_stub: PlaywrightStub
    ) -> None:
        playwright_stub.config["raise_on_goto"] = FakePlaywrightError("crashed")
        with browser.session():
            result = browser.render("https://shop.test/p/1", CTX)

        assert isinstance(result, FetchFailure)
        assert playwright_stub[0].chromium.pages_closed == 1

    def test_renders_outside_a_session_still_launch_their_own(
        self, playwright_stub: PlaywrightStub
    ) -> None:
        """The one-shot path is unchanged; every existing caller keeps its behaviour."""
        browser.render("https://shop.test/p/1", CTX)
        browser.render("https://shop.test/p/2", CTX)

        assert len(playwright_stub) == 2

    def test_the_session_does_not_leak_past_its_block(
        self, playwright_stub: PlaywrightStub
    ) -> None:
        with browser.session():
            browser.render("https://shop.test/p/1", CTX)
        browser.render("https://shop.test/p/2", CTX)

        # Second render launched its own, rather than reusing a closed browser.
        assert len(playwright_stub) == 2

    def test_a_session_without_playwright_is_not_an_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Callers write the ``with`` block without caring whether the extra is installed."""
        monkeypatch.setitem(sys.modules, "playwright.sync_api", None)

        with browser.session() as active:
            assert not active.available
            result = browser.render("https://shop.test/p/1", CTX)

        assert isinstance(result, FetchFailure)
        assert "browser extra" in result.message


class TestWaitFor:
    def test_waits_for_the_named_selector(self, playwright_stub: PlaywrightStub) -> None:
        browser.render("https://shop.test/s?q=x", CTX, wait_for="div.result")

        chromium = playwright_stub[0].chromium
        assert chromium.waited_for == ["div.result"]
        # Found it, so no blind settle was needed.
        assert chromium.settled == 0

    def test_falls_back_to_settling_when_the_selector_never_appears(
        self, playwright_stub: PlaywrightStub
    ) -> None:
        """A missing selector is not a failure.

        The page may genuinely not hold what we hoped for, and that is for the parser to
        report -- turning a readable-but-different page into a timeout would hide it.
        """
        playwright_stub.config["missing_selectors"] = frozenset({"div.result"})

        result = browser.render("https://shop.test/s?q=x", CTX, wait_for="div.result")

        assert isinstance(result, FetchSuccess)
        assert playwright_stub[0].chromium.settled == 1

    def test_without_a_selector_it_settles_as_before(
        self, playwright_stub: PlaywrightStub
    ) -> None:
        browser.render("https://shop.test/p/1", CTX)

        chromium = playwright_stub[0].chromium
        assert chromium.waited_for == []
        assert chromium.settled == 1
