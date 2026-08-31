"""Console output helpers.

The regression pinned here: prices are formatted with currency symbols, and a Windows
console defaults to cp1252, which cannot encode them. Printing a rupee price used to raise
``UnicodeEncodeError`` and abort the command.
"""

from __future__ import annotations

import io
from decimal import Decimal

from rich.console import Console

from product_tracker.cli.formatting import ExitCode, _use_utf8, table
from product_tracker.utils.money import format_money


class TestExitCodes:
    def test_success_is_zero(self) -> None:
        assert ExitCode.OK == 0

    def test_codes_are_distinct(self) -> None:
        values = [code.value for code in ExitCode]
        assert len(values) == len(set(values))

    def test_store_failure_is_distinguishable_from_a_crash(self) -> None:
        """Scripts branch on this: retry later vs investigate."""
        assert ExitCode.STORE_FAILURE != ExitCode.ERROR


class TestCurrencySymbolOutput:
    def test_rupee_price_survives_a_cp1252_stream(self) -> None:
        """The exact crash: a legacy Windows code page meeting U+20B9."""
        raw = io.TextIOWrapper(io.BytesIO(), encoding="cp1252", errors="strict")
        _use_utf8(raw)

        console = Console(file=raw, width=80)
        console.print(format_money(Decimal("69999"), "INR"))

        raw.flush()
        assert "69,999.00" in raw.buffer.getvalue().decode("utf-8")  # type: ignore[attr-defined]

    def test_reconfigure_switches_the_encoding(self) -> None:
        raw = io.TextIOWrapper(io.BytesIO(), encoding="cp1252")

        _use_utf8(raw)

        assert raw.encoding.lower().replace("-", "") == "utf8"

    def test_streams_without_reconfigure_are_left_alone(self) -> None:
        """A pipe under test capture must not blow up the CLI at import time."""

        class Bare:
            pass

        _use_utf8(Bare())  # type: ignore[arg-type]

    def test_none_stream_is_tolerated(self) -> None:
        """sys.stdout can be None in a windowed process."""
        _use_utf8(None)


class TestTable:
    def test_builds_with_the_given_columns(self) -> None:
        result = table("Products", ["ID", "Name"])
        assert result.title == "Products"
        assert len(result.columns) == 2
