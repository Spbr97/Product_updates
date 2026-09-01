"""URL validation, SSRF guard, and canonicalisation."""

from __future__ import annotations

import pytest

from product_tracker.domain.errors import InvalidURLError, UnsafeURLError
from product_tracker.utils.urls import (
    canonicalize_url,
    host_of,
    redact_urls,
    validate_url,
)


class TestValidation:
    @pytest.mark.parametrize(
        "url",
        [
            "https://www.flipkart.com/p/itm123",
            "http://example.com/product",
            "https://example.com:8443/p",
        ],
    )
    def test_accepts_well_formed_urls(self, url: str) -> None:
        assert validate_url(url, block_private=False) == url

    def test_strips_surrounding_whitespace(self) -> None:
        assert validate_url("  https://example.com/p  ", block_private=False) == (
            "https://example.com/p"
        )

    @pytest.mark.parametrize("url", ["", "   "])
    def test_rejects_empty(self, url: str) -> None:
        with pytest.raises(InvalidURLError, match="must not be empty"):
            validate_url(url, block_private=False)

    def test_rejects_overlong(self) -> None:
        with pytest.raises(InvalidURLError, match="exceeds"):
            validate_url("https://example.com/" + "a" * 3000, block_private=False)

    @pytest.mark.parametrize(
        "url",
        [
            "ftp://example.com/f",
            "file:///etc/passwd",
            "javascript:alert(1)",
            "data:text/html,<h1>x</h1>",
            "gopher://example.com/",
        ],
    )
    def test_rejects_disallowed_schemes(self, url: str) -> None:
        with pytest.raises(InvalidURLError):
            validate_url(url, block_private=False)

    def test_rejects_missing_scheme(self) -> None:
        with pytest.raises(InvalidURLError, match="scheme"):
            validate_url("www.example.com/p", block_private=False)

    def test_rejects_missing_host(self) -> None:
        with pytest.raises(InvalidURLError, match="host"):
            validate_url("https:///path", block_private=False)

    def test_rejects_embedded_credentials(self) -> None:
        """Credentials in a URL would be persisted and logged."""
        with pytest.raises(InvalidURLError, match="credentials"):
            validate_url("https://user:secret@example.com/p", block_private=False)


class TestSsrfGuard:
    @pytest.mark.parametrize(
        "url",
        [
            "http://127.0.0.1/admin",
            "http://localhost/admin",
            "http://0.0.0.0/",
            "http://10.0.0.5/internal",
            "http://192.168.1.1/router",
            "http://172.16.0.1/",
            "http://169.254.169.254/latest/meta-data/",  # cloud metadata service
            "http://[::1]/admin",
            "http://[fe80::1]/",
        ],
    )
    def test_rejects_private_and_loopback(self, url: str) -> None:
        with pytest.raises(UnsafeURLError):
            validate_url(url, block_private=True)

    def test_rejects_ipv4_mapped_ipv6_loopback(self) -> None:
        """``::ffff:127.0.0.1`` is loopback wearing an IPv6 hat; unwrap before judging."""
        with pytest.raises(UnsafeURLError):
            validate_url("http://[::ffff:127.0.0.1]/", block_private=True)

    def test_cloud_metadata_is_blocked(self) -> None:
        """The single most valuable SSRF target on a cloud host."""
        with pytest.raises(UnsafeURLError, match="non-public"):
            validate_url("http://169.254.169.254/", block_private=True)

    def test_allows_public_literal(self) -> None:
        assert validate_url("https://1.1.1.1/", block_private=True)

    def test_guard_can_be_disabled(self) -> None:
        """Opt-out exists for deliberately tracking an internal host."""
        assert validate_url("http://127.0.0.1/p", block_private=False)

    def test_unresolvable_host_is_rejected(self) -> None:
        with pytest.raises(InvalidURLError, match="cannot resolve"):
            validate_url("https://nonexistent.invalid/p", block_private=True)


class TestCanonicalisation:
    def test_lowercases_scheme_and_host_but_not_path(self) -> None:
        assert canonicalize_url("HTTPS://WWW.Example.COM/Product/ABC") == (
            "https://www.example.com/Product/ABC"
        )

    def test_drops_fragment(self) -> None:
        assert canonicalize_url("https://example.com/p#reviews") == "https://example.com/p"

    def test_drops_default_ports(self) -> None:
        assert canonicalize_url("https://example.com:443/p") == "https://example.com/p"
        assert canonicalize_url("http://example.com:80/p") == "http://example.com/p"

    def test_keeps_non_default_port(self) -> None:
        assert canonicalize_url("https://example.com:8443/p") == "https://example.com:8443/p"

    def test_drops_trailing_slash(self) -> None:
        assert canonicalize_url("https://example.com/p/") == "https://example.com/p"

    def test_keeps_bare_root(self) -> None:
        assert canonicalize_url("https://example.com/") == "https://example.com/"

    @pytest.mark.parametrize(
        "param",
        ["utm_source=x", "utm_campaign=y", "gclid=abc", "fbclid=def", "lid=LSTX", "otracker=z"],
    )
    def test_strips_tracking_parameters(self, param: str) -> None:
        assert canonicalize_url(f"https://example.com/p?{param}") == "https://example.com/p"

    def test_keeps_identifying_parameters(self) -> None:
        """Flipkart's ``pid`` selects the variant -- stripping it would merge two products."""
        assert canonicalize_url("https://www.flipkart.com/x/p/itm1?pid=ABC&lid=XYZ") == (
            "https://www.flipkart.com/x/p/itm1?pid=ABC"
        )

    def test_sorts_remaining_parameters(self) -> None:
        assert canonicalize_url("https://example.com/p?b=2&a=1") == (
            "https://example.com/p?a=1&b=2"
        )

    def test_two_shares_of_one_listing_collapse(self) -> None:
        """The point of canonicalisation: these are the same product."""
        first = "https://www.flipkart.com/apple-iphone-17/p/itm6eb?pid=MOB123&lid=LSTA&marketplace=FLIPKART&utm_source=share"
        second = "https://www.flipkart.com/apple-iphone-17/p/itm6eb?pid=MOB123&otracker=search&fm=organic"
        assert canonicalize_url(first) == canonicalize_url(second)

    def test_different_variants_stay_distinct(self) -> None:
        black = canonicalize_url("https://www.flipkart.com/x/p/itm1?pid=BLACK256")
        blue = canonicalize_url("https://www.flipkart.com/x/p/itm1?pid=BLUE256")
        assert black != blue

    def test_is_idempotent(self) -> None:
        once = canonicalize_url("https://Example.com/p/?utm_source=x&b=2&a=1#frag")
        assert canonicalize_url(once) == once


class TestHostOf:
    @pytest.mark.parametrize(
        ("url", "expected"),
        [
            ("https://WWW.Flipkart.com/p", "www.flipkart.com"),
            ("https://example.com:8443/p", "example.com"),
            ("not a url", ""),
            ("", ""),
        ],
    )
    def test_extracts_lowercase_host(self, url: str, expected: str) -> None:
        assert host_of(url) == expected


class TestRedactUrls:
    """Error text from HTTP clients is stored in the database and written to logs."""

    def test_strips_the_query_string(self) -> None:
        assert redact_urls("failed loading https://shop.test/p/1?token=SECRET") == (
            "failed loading https://shop.test/..."
        )

    def test_strips_embedded_credentials(self) -> None:
        """validate_url refuses these, but a redirect chain can still produce one."""
        result = redact_urls("connecting to https://user:hunter2@shop.test/p/1")

        assert "hunter2" not in result
        assert "user" not in result
        assert "shop.test" in result

    def test_keeps_the_host_for_diagnosis(self) -> None:
        assert "shop.test" in redact_urls("timeout for https://shop.test/p/1?a=b")

    def test_handles_several_urls(self) -> None:
        result = redact_urls("https://a.test/x?k=1 redirected to https://b.test/y?k=2")

        assert "k=1" not in result
        assert "k=2" not in result
        assert "a.test" in result
        assert "b.test" in result

    def test_leaves_text_without_urls_alone(self) -> None:
        assert redact_urls("connection reset by peer") == "connection reset by peer"

    def test_handles_a_bare_host(self) -> None:
        assert redact_urls("https://shop.test") == "https://shop.test/..."

    @pytest.mark.parametrize("scheme", ["http", "https", "HTTPS"])
    def test_both_schemes_and_any_case(self, scheme: str) -> None:
        assert "SECRET" not in redact_urls(f"{scheme}://shop.test/p?token=SECRET")


class TestPaidClickUrls:
    """Canonicalisation against URLs as they actually arrive from a shopping ad.

    Every one of these is a real URL a person pasted in. They are the hard case for
    duplicate detection: the same listing reached through two ads differs in a dozen
    parameters, and ``url_canonical`` is the only thing standing between that and the same
    product being tracked twice.
    """

    def test_a_flipkart_page_uid_does_not_defeat_deduplication(self) -> None:
        """``pageUID`` is a millisecond timestamp, so it differs on every single visit.

        Left in, the same listing shared twice canonicalises to two different URLs and is
        tracked as two products, each fetched separately from Flipkart.
        """
        base = "https://www.flipkart.com/samsung-galaxy-s25-5g-navy-256-gb/p/itm277a7d18"
        first = canonicalize_url(f"{base}?pid=MOBH8K8U4ZPHSNKK&pageUID=1788269621508")
        second = canonicalize_url(f"{base}?pid=MOBH8K8U4ZPHSNKK&pageUID=1788269999999")

        assert first == second

    def test_flipkart_keeps_the_variant_id(self) -> None:
        """``pid`` selects the colour and capacity; stripping it would merge variants."""
        canonical = canonicalize_url(
            "https://www.flipkart.com/samsung-galaxy-s25-5g-navy-256-gb/p/itm277a7d18"
            "?pid=MOBH8K8U4ZPHSNKK&marketplace=FLIPKART&lid=LSTMOB&hl_lid=LSTX"
        )
        assert "pid=MOBH8K8U4ZPHSNKK" in canonical
        for stripped in ("marketplace", "lid", "hl_lid"):
            assert stripped not in canonical

    def test_google_ads_parameters_are_stripped(self) -> None:
        canonical = canonicalize_url(
            "https://www.vijaysales.com/p/P237290/237327/samsung-galaxy-s25-navy"
            "?utm_source=google&utm_medium=cpc&gad_source=1&gad_campaignid=23251475503"
            "&gbraid=0AAAAADLKtlk&gclid=Cj0KCQjw79nUBhCgARIsADSHka1WNb8"
        )
        assert canonical == (
            "https://www.vijaysales.com/p/P237290/237327/samsung-galaxy-s25-navy"
        )

    def test_amazon_ad_breadcrumbs_are_stripped_but_the_variation_is_kept(self) -> None:
        """``th`` selects a variation, so it stays; the hv* breadcrumbs do not."""
        canonical = canonicalize_url(
            "https://www.amazon.in/Samsung-Snapdragon/dp/B0H3FN92VB"
            "?mcid=28684655bf&tag=googleshopdes-21&linkCode=df0&hvadid=709962856229"
            "&hvpos=&hvnetw=g&hvtargid=pla-2494605164839&gad_source=1&th=1"
        )
        assert canonical.endswith("?th=1")
        for stripped in ("mcid", "tag", "linkCode", "hvadid", "hvnetw", "gad_source"):
            assert stripped not in canonical

    def test_a_campaign_id_is_stripped_but_a_model_code_is_kept(self) -> None:
        canonical = canonicalize_url(
            "https://www.samsung.com/in/smartphones/galaxy-s25/buy/"
            "?modelCodeSM-S931BLBC&cid=in_pd_pmax_google&gad_source=1&gclid=Cj0KCQ"
        )
        assert "modelCodeSM-S931BLBC" in canonical
        assert "cid=" not in canonical
