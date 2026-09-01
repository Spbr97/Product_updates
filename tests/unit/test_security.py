"""API key verification."""

from __future__ import annotations

import pytest

from product_tracker.core.config import Settings
from product_tracker.core.security import (
    is_auth_enabled,
    requires_key_for_reads,
    verify_api_key,
)

KEY = "s3cret-api-key"


def settings(**overrides: object) -> Settings:
    base = {"database_url": "postgresql://u:p@localhost:5432/db"}
    return Settings(**{**base, **overrides})  # type: ignore[arg-type]


class TestAuthDisabled:
    def test_not_enabled_without_a_key(self) -> None:
        assert not is_auth_enabled(settings())

    @pytest.mark.parametrize("presented", [None, "", "anything"])
    def test_everything_verifies(self, presented: str | None) -> None:
        """With no key configured there is nothing to fail."""
        assert verify_api_key(settings(), presented)

    def test_reads_never_need_a_key(self) -> None:
        assert not requires_key_for_reads(settings())


class TestAuthEnabled:
    def test_enabled_with_a_key(self) -> None:
        assert is_auth_enabled(settings(api_key=KEY))

    def test_correct_key_verifies(self) -> None:
        assert verify_api_key(settings(api_key=KEY), KEY)

    @pytest.mark.parametrize(
        "presented",
        [None, "", "wrong", "s3cret-api-ke", "s3cret-api-keyy", "S3CRET-API-KEY"],
    )
    def test_wrong_or_missing_key_fails(self, presented: str | None) -> None:
        assert not verify_api_key(settings(api_key=KEY), presented)

    def test_reads_are_anonymous_by_default(self) -> None:
        assert not requires_key_for_reads(settings(api_key=KEY))

    def test_reads_can_be_locked_down(self) -> None:
        assert requires_key_for_reads(
            settings(api_key=KEY, api_allow_anonymous_reads=False)
        )

    def test_locking_reads_without_a_key_does_nothing(self) -> None:
        """No key means no auth at all; the read flag alone cannot enable it."""
        assert not requires_key_for_reads(settings(api_allow_anonymous_reads=False))

    def test_the_key_is_not_exposed_by_repr(self) -> None:
        """SecretStr keeps the value out of logs and tracebacks."""
        assert KEY not in repr(settings(api_key=KEY).api_key)
