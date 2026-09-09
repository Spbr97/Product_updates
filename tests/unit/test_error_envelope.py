"""The error envelope's stable codes.

A client branches on ``error.type``. The string is therefore a contract, and it is the
kind of contract that breaks silently: nothing fails when a code is renamed or a subclass
starts reporting its parent's code -- the caller simply stops recognising the case and
falls into its default branch, usually "something went wrong", months later and far away.

So the map is asserted directly. Most of these codes cannot be produced through a route
in normal operation (``StoreError`` in particular is never raised to a handler: a store
failure is a recorded execution returned as 200, not an HTTP error), which is exactly why
they need a test that does not depend on finding a route that raises them.
"""

from __future__ import annotations

import pytest

from product_tracker.api.errors import _ERROR_MAP
from product_tracker.domain.errors import (
    ConfigurationError,
    DuplicateError,
    DuplicateListingError,
    InvalidStoreURLError,
    InvalidURLError,
    NoAdapterError,
    NotFoundError,
    StoreError,
    UnsafeURLError,
    ValidationError,
)


def code_for(exc: Exception) -> tuple[int, str]:
    """What the handler would report, resolved the way the handler resolves it."""
    for exc_type, status_code, error_type in _ERROR_MAP:
        if isinstance(exc, exc_type):
            return status_code, error_type
    raise AssertionError(f"{type(exc).__name__} is not in the map")


class TestCodes:
    @pytest.mark.parametrize(
        ("exc", "expected"),
        [
            (NotFoundError("Product", 1), (404, "not_found")),
            (DuplicateListingError("https://shop.example.com/p/1", 7), (409, "duplicate_listing")),
            (DuplicateError("Product", "https://shop.example.com/p/1"), (409, "conflict")),
            (NoAdapterError("no adapter"), (422, "unsupported_store")),
            (
                InvalidStoreURLError("amazon-in", "flipkart", "https://flipkart.com/p/1"),
                (422, "invalid_store_url"),
            ),
            (UnsafeURLError("non-public address"), (422, "ssrf_blocked")),
            (InvalidURLError("malformed"), (422, "validation_error")),
            (ValidationError("bad input"), (422, "validation_error")),
            (ConfigurationError("no DSN"), (500, "configuration_error")),
            (StoreError("the shop refused"), (502, "store_failure")),
        ],
    )
    def test_each_exception_reports_its_own_code(
        self, exc: Exception, expected: tuple[int, str]
    ) -> None:
        assert code_for(exc) == expected


class TestSubclassesPrecedeParents:
    """The map is scanned in order and the first match wins, so a subclass listed after
    its parent silently inherits the parent's code and can never be told apart."""

    def test_ssrf_is_not_reported_as_a_plain_validation_error(self) -> None:
        """The distinction that matters: a typo and a refused destination are different
        problems with different fixes."""
        assert code_for(UnsafeURLError("non-public address"))[1] == "ssrf_blocked"
        assert code_for(ValidationError("bad input"))[1] == "validation_error"

    def test_a_duplicate_listing_is_not_reported_as_a_plain_conflict(self) -> None:
        clash = DuplicateListingError("https://shop.example.com/p/1", 7)
        assert code_for(clash)[1] == "duplicate_listing"

    def test_every_subclass_in_the_map_precedes_its_parent(self) -> None:
        """Structural, so a future entry appended in the wrong place fails here rather
        than by a caller quietly losing a case."""
        types = [exc_type for exc_type, _, _ in _ERROR_MAP]
        for index, exc_type in enumerate(types):
            for later in types[index + 1 :]:
                assert not issubclass(later, exc_type), (
                    f"{later.__name__} is a subclass of {exc_type.__name__} but is listed "
                    f"after it, so it can never match: move it earlier"
                )


class TestCodesAreStable:
    def test_the_contract_s_types_are_all_reachable(self) -> None:
        """Every code the contract names must be produced by something.

        `forbidden` and `unauthorized` are deliberately absent: authentication is handled
        before the domain layer (401 from the dependency), and cross-account access is
        reported as 404 rather than 403 so that ids cannot be enumerated by watching which
        ones answer differently.
        """
        produced = {error_type for _, _, error_type in _ERROR_MAP}

        assert {
            "not_found", "conflict", "duplicate_listing", "unsupported_store",
            "invalid_store_url", "ssrf_blocked", "validation_error", "store_failure",
        } <= produced
