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

    def wait_for_timeout(self, _ms: int) -> None: ...

    def content(self) -> str:
        return self.owner.html


class FakeBrowser:
    def __init__(self, owner: FakeChromium) -> None:
        self.owner = owner
        self.html = owner.html
        self.status = owner.status
        self.final_url = owner.final_url
        self.visited = owner.visited
        self.raise_on_goto = owner.raise_on_goto

    def new_page(self, **_kwargs: object) -> FakePage:
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

    def launch(self, **_kwargs: object) -> FakeBrowser:
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
    ) -> None:
        self.html = html
        self.status = status
        self.final_url = final_url
        self.raise_on_goto = raise_on_goto
        self.visited: list[str] = []
        self.chromium = FakeChromium(self)
        self.browser: FakeBrowser | None = None

    def __enter__(self) -> FakePlaywright:
        return self

    def __exit__(self, *_args: object) -> None: ...

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
