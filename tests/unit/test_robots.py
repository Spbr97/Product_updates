"""Reading robots.txt, and doing what it says.

These exist because the project got this wrong. A Flipkart search was shipped against
``/search?q=``, which Flipkart's robots.txt disallows in as many words, and the 403s that
came back were read as rate limiting rather than as the site declining.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from product_tracker.domain.enums import FetchOutcome
from product_tracker.domain.models import FetchContext
from product_tracker.stores import robots
from product_tracker.stores.http import FetchFailure, FetchSuccess

CTX = FetchContext(user_agent="product-tracker-test", verify_public_host=False)

FLIPKART_STYLE = "\n".join(
    ["User-agent: *", "Disallow: /search?", "Disallow: /reviews/", "Allow: /"]
)


@pytest.fixture(autouse=True)
def _clear_cache() -> Iterator[None]:
    robots.reset_cache()
    yield
    robots.reset_cache()


class Fetcher:
    """Stands in for the HTTP client, and counts calls."""

    def __init__(self, body: str | None = FLIPKART_STYLE, status: int = 200) -> None:
        self.body = body
        self.status = status
        self.calls: list[str] = []

    def __call__(self, url: str, ctx: FetchContext) -> FetchSuccess | FetchFailure:
        self.calls.append(url)
        if self.body is None:
            return FetchFailure(FetchOutcome.HTTP_ERROR, "not found", http_status=self.status)
        return FetchSuccess(html=self.body, url=url, http_status=self.status)


def install(monkeypatch: pytest.MonkeyPatch, fetcher: Fetcher) -> Fetcher:
    monkeypatch.setattr(robots, "http_fetch", fetcher)
    return fetcher


class TestObeyingTheRules:
    def test_a_disallowed_search_path_is_refused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        install(monkeypatch, Fetcher())
        assert not robots.is_allowed("https://www.flipkart.com/search?q=galaxy", CTX)

    def test_an_allowed_product_path_is_permitted(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The rule is about crawling search, not about pages a person named."""
        install(monkeypatch, Fetcher())
        assert robots.is_allowed("https://www.flipkart.com/samsung/p/itm123", CTX)

    def test_robots_is_read_from_the_site_root(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fetcher = install(monkeypatch, Fetcher())
        robots.is_allowed("https://www.flipkart.com/search?q=x", CTX)
        assert fetcher.calls == ["https://www.flipkart.com/robots.txt"]


class TestUnreadableRules:
    def test_a_missing_robots_allows_everything(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A 404 means no restrictions, not a prohibition."""
        install(monkeypatch, Fetcher(body=None, status=404))
        assert robots.is_allowed("https://shop.test/search?q=x", CTX)

    def test_an_unreachable_robots_does_not_block_everything(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The trap that made this whole thing hard to diagnose.

        ``RobotFileParser.read`` fetches with urllib's default user agent, which several
        retailers refuse -- and on a failed read the parser answers "disallowed" for every
        path including "/". From the outside that is indistinguishable from a site banning
        you outright.
        """
        install(monkeypatch, Fetcher(body=None, status=503))
        assert robots.is_allowed("https://shop.test/", CTX)
        assert robots.is_allowed("https://shop.test/search?q=x", CTX)


class TestCaching:
    def test_one_fetch_serves_many_questions(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A fan-out must not re-fetch robots.txt per query."""
        fetcher = install(monkeypatch, Fetcher())
        for _ in range(5):
            robots.is_allowed("https://www.flipkart.com/search?q=x", CTX)
        assert len(fetcher.calls) == 1

    def test_each_host_is_asked_separately(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fetcher = install(monkeypatch, Fetcher())
        robots.is_allowed("https://a.test/search?q=x", CTX)
        robots.is_allowed("https://b.test/search?q=x", CTX)
        assert len(fetcher.calls) == 2

    def test_the_cache_can_be_cleared(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fetcher = install(monkeypatch, Fetcher())
        robots.is_allowed("https://a.test/search?q=x", CTX)
        robots.reset_cache()
        robots.is_allowed("https://a.test/search?q=x", CTX)
        assert len(fetcher.calls) == 2


class TestSearchHonoursIt:
    def test_a_disallowed_store_reports_disallowed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import product_tracker.stores.search as search_module
        from product_tracker.domain.enums import SearchOutcome
        from product_tracker.stores.search import ConfiguredSearch

        monkeypatch.setattr(search_module.robots, "is_allowed", lambda url, ctx: False)
        called: list[str] = []
        monkeypatch.setattr(
            search_module, "http_fetch", lambda url, ctx: called.append(url)  # type: ignore[arg-type,return-value]
        )

        result = ConfiguredSearch("flipkart").search("galaxy s25", CTX)

        assert result.outcome is SearchOutcome.DISALLOWED
        # And crucially: no request was made.
        assert called == []


class TestGroupsThatApplyToUs:
    """Which rules apply, worked out here rather than left to the standard library.

    Flipkart states its wildcard rules in eleven separate groups. RFC 9309 says groups
    sharing a user-agent are merged; ``urllib.robotparser`` did so on Python 3.14 and did
    not on 3.12. Byte-identical file, identical user agent, opposite answers -- and the
    deployed image ran 3.12, so the permissive answer was the one that counted and the
    tool fetched a path the site disallows.
    """

    def test_every_wildcard_group_applies(self) -> None:
        body = "\n".join(
            [
                "User-agent: *",
                "Disallow: /search?",
                "",
                "User-agent: *",
                "Disallow: /ps/",
                "",
                "User-agent: *",
                "Disallow: /reviews/",
            ]
        )
        rules = robots.rules_for(body, "product-tracker-test")

        assert "Disallow: /search?" in rules
        assert "Disallow: /ps/" in rules  # the group a non-merging parser dropped
        assert "Disallow: /reviews/" in rules

    def test_a_group_naming_us_replaces_the_wildcard(self) -> None:
        """"Most specific group wins" -- the wildcard is not also applied on top."""
        body = "\n".join(
            [
                "User-agent: *",
                "Disallow: /",
                "",
                "User-agent: product-tracker",
                "Disallow: /admin/",
            ]
        )
        rules = robots.rules_for(body, "product-tracker-test/1.0")

        assert "Disallow: /admin/" in rules
        assert "Disallow: /" not in rules

    def test_consecutive_user_agent_lines_share_one_group(self) -> None:
        body = "\n".join(
            ["User-agent: googlebot", "User-agent: *", "Disallow: /private/"]
        )
        assert "Disallow: /private/" in robots.rules_for(body, "product-tracker-test")

    def test_rules_for_other_agents_are_not_ours(self) -> None:
        body = "\n".join(["User-agent: googlebot", "Disallow: /"])
        rules = robots.rules_for(body, "product-tracker-test")

        assert "Disallow: /" not in rules

    def test_comments_and_non_rule_fields_are_ignored(self) -> None:
        body = "\n".join(
            [
                "# a comment",
                "User-agent: *",
                "Crawl-delay: 10",
                "Disallow: /search?   # inline comment",
                "Sitemap: https://example.com/sitemap.xml",
            ]
        )
        rules = robots.rules_for(body, "product-tracker-test")

        assert rules == ["User-agent: *", "Disallow: /search?"]

    def test_a_file_saying_nothing_about_us_permits_everything(self) -> None:
        assert robots.rules_for("", "product-tracker-test") == ["User-agent: *"]


class TestRulesComeOutMostSpecificFirst:
    """Ordering is load-bearing, because the parsers disagree about precedence.

    Given ``Allow: /`` and ``Disallow: /search?`` and asked about ``/search?q=x``,
    Python 3.12 takes the first matching rule (Allow) while 3.13 and 3.14 take the
    longest match (Disallow). Only one of those obeys RFC 9309, and on 3.12 the
    difference meant crawling a path the site asked us to leave alone -- so the order is
    fixed here rather than left to the interpreter.
    """

    def test_the_longer_path_is_emitted_first(self) -> None:
        body = "\n".join(["User-agent: *", "Allow: /", "Disallow: /search?"])

        assert robots.rules_for(body, "product-tracker-test") == [
            "User-agent: *",
            "Disallow: /search?",
            "Allow: /",
        ]

    def test_allow_wins_a_tie(self) -> None:
        """RFC 9309: at equal length, the least restrictive rule wins."""
        body = "\n".join(["User-agent: *", "Disallow: /abc", "Allow: /abc"])

        assert robots.rules_for(body, "product-tracker-test")[1] == "Allow: /abc"


class TestMergedRulesAreEnforced:
    """The point of merging: the verdict must not depend on the interpreter."""

    #: The shape of Flipkart's file -- the disallow that matters is not in the first group.
    REPEATED = "\n".join(
        [
            "User-agent: *",
            "Allow: /",
            "",
            "User-agent: *",
            "Disallow: /search?",
        ]
    )

    def test_a_disallow_in_a_later_group_is_honoured(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            robots,
            "http_fetch",
            lambda url, ctx: FetchSuccess(
                url=url, html=self.REPEATED, http_status=200
            ),
        )

        assert not robots.is_allowed("https://shop.example.com/search?q=x", CTX)

    def test_product_pages_stay_allowed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Merging must not over-block: tracking a listing has to keep working."""
        monkeypatch.setattr(
            robots,
            "http_fetch",
            lambda url, ctx: FetchSuccess(
                url=url, html=self.REPEATED, http_status=200
            ),
        )

        assert robots.is_allowed("https://shop.example.com/p/itm123", CTX)
