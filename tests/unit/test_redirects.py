"""Where a redirect is allowed to lead.

The SSRF guard was written for the URL a user hands us. A redirect is the way around it:
the shop's URL is perfectly public, and the 302 it answers with is not. The project's own
audit checklist names "invalid redirect" and "private redirect" as cases that must be
handled, and until these tests existed nothing in the suite touched a redirect at all --
the only match for the word was a test about redacting credentials from error text.

The distinction these pin down is between *refusing the answer* and *not asking*. The
code used to read the final URL after ``httpx`` had followed the chain, which caught the
data but not the request: a GET to ``169.254.169.254`` had already gone out, and for a
metadata endpoint or an internal admin route, sending it is the whole attack. Discarding
the response afterwards does not recall it.

Addresses here are IP literals so that nothing has to be resolved -- ``assert_public_host``
parses a literal directly, which keeps these tests off DNS as well as off the network.
"""

from __future__ import annotations

from collections.abc import Iterator

import httpx
import pytest
import respx

from product_tracker.domain.enums import FetchOutcome
from product_tracker.domain.models import FetchContext
from product_tracker.stores import http
from product_tracker.stores.http import FetchFailure, FetchSuccess

#: A public literal, so the first hop is legitimate and the redirect is what is on trial.
PUBLIC = "https://93.184.216.34/p/1"
ELSEWHERE_PUBLIC = "https://93.184.216.35/p/2"

#: The three shapes of "somewhere we must not go", one per address class that matters.
LINK_LOCAL = "http://169.254.169.254/latest/meta-data/iam/security-credentials/"
LOOPBACK = "http://127.0.0.1:8000/api/v1/products"
PRIVATE_LAN = "http://10.0.0.5/admin"

VERIFYING = FetchContext(timeout_seconds=5, verify_public_host=True)


@pytest.fixture(autouse=True)
def _respx_router() -> Iterator[None]:
    # Bare ``respx.mock``, not ``respx.mock(...)``: the parametrised form builds a
    # separate router, and ``respx.get(...)`` would keep registering on the default one.
    with respx.mock:
        yield


def redirect_to(target: str) -> httpx.Response:
    return httpx.Response(302, headers={"Location": target})


class TestARedirectSomewhereInternal:
    @pytest.mark.parametrize(
        ("name", "target"),
        [("metadata service", LINK_LOCAL), ("loopback", LOOPBACK), ("private LAN", PRIVATE_LAN)],
    )
    def test_it_is_refused(self, name: str, target: str) -> None:
        respx.get(PUBLIC).mock(return_value=redirect_to(target))
        respx.get(target).mock(return_value=httpx.Response(200, text="secrets"))

        result = http.fetch(PUBLIC, VERIFYING)

        assert isinstance(result, FetchFailure), name
        assert result.outcome is FetchOutcome.ERROR
        assert "refused for safety" in result.message

    @pytest.mark.parametrize("target", [LINK_LOCAL, LOOPBACK, PRIVATE_LAN])
    def test_the_internal_request_is_never_sent(self, target: str) -> None:
        """The point of the exercise.

        Refusing the response is not enough. A blind SSRF is still an SSRF: an internal
        endpoint that acts on a GET has already acted by the time we decide to ignore what
        it said.
        """
        respx.get(PUBLIC).mock(return_value=redirect_to(target))
        inward = respx.get(target).mock(return_value=httpx.Response(200, text="secrets"))

        http.fetch(PUBLIC, VERIFYING)

        assert inward.call_count == 0

    def test_the_response_body_never_reaches_the_caller(self) -> None:
        respx.get(PUBLIC).mock(return_value=redirect_to(LINK_LOCAL))
        respx.get(LINK_LOCAL).mock(return_value=httpx.Response(200, text="AKIA-EXAMPLE-KEY"))

        result = http.fetch(PUBLIC, VERIFYING)

        assert "AKIA-EXAMPLE-KEY" not in (getattr(result, "html", "") or "")
        assert "AKIA-EXAMPLE-KEY" not in (getattr(result, "message", "") or "")

    def test_a_chain_that_turns_inward_halfway_is_stopped_there(self) -> None:
        """The second hop is public and the third is not. Checking only the first and the
        last would let this through if the last one redirected back out again."""
        respx.get(PUBLIC).mock(return_value=redirect_to(ELSEWHERE_PUBLIC))
        respx.get(ELSEWHERE_PUBLIC).mock(return_value=redirect_to(PRIVATE_LAN))
        inward = respx.get(PRIVATE_LAN).mock(return_value=httpx.Response(200, text="admin"))

        result = http.fetch(PUBLIC, VERIFYING)

        assert isinstance(result, FetchFailure)
        assert inward.call_count == 0


class TestARedirectSomewhereLegitimate:
    def test_it_is_followed(self) -> None:
        """Shops redirect constantly -- canonical URLs, country splits, tracking
        parameters being stripped. Blocking redirects outright would break tracking."""
        respx.get(PUBLIC).mock(return_value=redirect_to(ELSEWHERE_PUBLIC))
        respx.get(ELSEWHERE_PUBLIC).mock(
            return_value=httpx.Response(200, html="<html><body>the product</body></html>")
        )

        result = http.fetch(PUBLIC, VERIFYING)

        assert isinstance(result, FetchSuccess)
        assert "the product" in result.html

    def test_the_final_url_is_what_gets_reported(self) -> None:
        """Not the one we asked for. Everything downstream -- canonicalisation, the audit
        row when a listing moves -- keys off where we actually landed."""
        respx.get(PUBLIC).mock(return_value=redirect_to(ELSEWHERE_PUBLIC))
        respx.get(ELSEWHERE_PUBLIC).mock(return_value=httpx.Response(200, html="<html></html>"))

        result = http.fetch(PUBLIC, VERIFYING)

        assert isinstance(result, FetchSuccess)
        assert result.url == ELSEWHERE_PUBLIC


class TestARedirectLoop:
    def test_it_ends_as_a_failure_rather_than_a_hang(self) -> None:
        respx.get(PUBLIC).mock(return_value=redirect_to(ELSEWHERE_PUBLIC))
        respx.get(ELSEWHERE_PUBLIC).mock(return_value=redirect_to(PUBLIC))

        result = http.fetch(PUBLIC, VERIFYING)

        assert isinstance(result, FetchFailure)
        assert result.outcome is FetchOutcome.HTTP_ERROR
        assert "too many redirects" in result.message


class TestWhenVerificationIsOff:
    def test_redirects_are_not_policed(self) -> None:
        """The unit fixtures use hostnames that resolve nowhere, so they turn the guard
        off. This pins that the switch is the only thing deciding it -- otherwise a
        change here would quietly alter what every other test is exercising.
        """
        respx.get(PUBLIC).mock(return_value=redirect_to(PRIVATE_LAN))
        inward = respx.get(PRIVATE_LAN).mock(return_value=httpx.Response(200, html="<html/>"))

        result = http.fetch(PUBLIC, FetchContext(timeout_seconds=5, verify_public_host=False))

        assert isinstance(result, FetchSuccess)
        assert inward.call_count == 1


class TestBytesTakeTheSameRoute:
    def test_a_sitemap_download_is_guarded_too(self) -> None:
        """``fetch_bytes`` is how catalogues are pulled, on a URL read out of a robots.txt
        -- which is a file the shop writes, not us. It shares ``_fetch_raw``, and this is
        the test that says so out loud."""
        respx.get(PUBLIC).mock(return_value=redirect_to(LINK_LOCAL))
        inward = respx.get(LINK_LOCAL).mock(return_value=httpx.Response(200, text="secrets"))

        result = http.fetch_bytes(PUBLIC, VERIFYING)

        assert isinstance(result, FetchFailure)
        assert "refused for safety" in result.message
        assert inward.call_count == 0
