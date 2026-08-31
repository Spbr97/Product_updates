"""Structured-data extraction from saved pages.

Every case reads a fixture file. Nothing here touches the network, so the suite does not
depend on any retailer being online or on their markup staying still.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from product_tracker.domain.enums import Availability
from product_tracker.stores import extraction

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"
BASE = "https://example.com/product/1"


def load(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


class TestJsonLd:
    def test_reads_price_name_and_availability(self) -> None:
        data = extraction.from_json_ld(load("jsonld_in_stock.html"), BASE)

        assert data is not None
        assert data.name == "Apple iPhone 17 (Black, 256 GB)"
        assert data.price == Decimal("69999.00")
        assert data.currency == "INR"
        assert data.availability is Availability.IN_STOCK
        assert data.identifier == "MOBHFN6YN2HXB5HE"

    def test_resolves_relative_image_against_base(self) -> None:
        data = extraction.from_json_ld(load("jsonld_in_stock.html"), BASE)
        assert data is not None
        assert data.image_url == "https://example.com/images/iphone17-black.jpg"

    def test_finds_product_nested_in_graph(self) -> None:
        """JSON-LD nests products under @graph, mainEntity, itemListElement..."""
        data = extraction.from_json_ld(load("jsonld_graph_nested.html"), BASE)

        assert data is not None
        assert data.name == "Sony WH-1000XM5"
        assert data.price == Decimal("26990")
        assert data.identifier == "WH1000XM5B"

    def test_out_of_stock_offer_has_no_price(self) -> None:
        data = extraction.from_json_ld(load("jsonld_out_of_stock.html"), BASE)

        assert data is not None
        assert data.availability is Availability.OUT_OF_STOCK
        assert data.price is None

    def test_missing_availability_field_is_unknown_not_in_stock(self) -> None:
        """A price is not evidence of stock. Inventing IN_STOCK causes false alerts."""
        data = extraction.from_json_ld(load("jsonld_no_availability.html"), BASE)

        assert data is not None
        assert data.price == Decimal("14999")
        assert data.availability is Availability.UNKNOWN

    def test_malformed_json_is_skipped(self) -> None:
        html = '<script type="application/ld+json">{not json</script>'
        assert extraction.from_json_ld(html, BASE) is None


class TestAvailabilityMapping:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("https://schema.org/InStock", Availability.IN_STOCK),
            ("http://schema.org/InStock", Availability.IN_STOCK),
            ("InStock", Availability.IN_STOCK),
            ("instock", Availability.IN_STOCK),
            ("LimitedAvailability", Availability.IN_STOCK),
            ("InStoreOnly", Availability.IN_STOCK),
            ("https://schema.org/OutOfStock", Availability.OUT_OF_STOCK),
            ("SoldOut", Availability.OUT_OF_STOCK),
            ("Discontinued", Availability.UNAVAILABLE),
        ],
    )
    def test_maps_known_values(self, raw: str, expected: Availability) -> None:
        assert extraction.parse_availability(raw) == expected

    @pytest.mark.parametrize("raw", ["PreOrder", "BackOrder", "PreSale"])
    def test_preorder_is_unknown_not_a_stock_claim(self, raw: str) -> None:
        """Orderable but not stocked. Calling it either way would be wrong."""
        assert extraction.parse_availability(raw) is Availability.UNKNOWN

    @pytest.mark.parametrize("raw", [None, "", "   ", "SomethingNew", 42])
    def test_unrecognised_is_unknown(self, raw: object) -> None:
        assert extraction.parse_availability(raw) is Availability.UNKNOWN


class TestMetaTags:
    def test_reads_opengraph_product(self) -> None:
        data = extraction.from_meta_tags(load("opengraph_product.html"), BASE)

        assert data is not None
        assert data.name == "Croma 32 inch HD TV"
        assert data.price == Decimal("12499.00")
        assert data.currency == "INR"
        assert data.availability is Availability.IN_STOCK
        assert data.image_url == "https://example.com/media/tv.png"

    def test_returns_none_without_product_tags(self) -> None:
        assert extraction.from_meta_tags(load("no_product.html"), BASE) is None


class TestLabelledText:
    def test_reads_a_labelled_price(self) -> None:
        data = extraction.from_labelled_text(load("labelled_price.html"), BASE)

        assert data is not None
        assert data.price == Decimal("4999")
        assert data.name == "Vijay Sales Mixer Grinder"

    def test_does_not_pick_up_the_emi_figure(self) -> None:
        """The page also says 'EMI from 250 per month'; only the labelled price counts."""
        data = extraction.from_labelled_text(load("labelled_price.html"), BASE)
        assert data is not None
        assert data.price != Decimal("250")

    def test_availability_stays_unknown(self) -> None:
        """Visible text tells us nothing about stock."""
        data = extraction.from_labelled_text(load("labelled_price.html"), BASE)
        assert data is not None
        assert data.availability is Availability.UNKNOWN

    def test_requires_a_label(self) -> None:
        html = "<html><body><h1>Thing</h1><div>4999</div></body></html>"
        assert extraction.from_labelled_text(html, BASE) is None


class TestExtractPreference:
    def test_prefers_json_ld_over_meta(self) -> None:
        html = (
            '<script type="application/ld+json">'
            '{"@type":"Product","name":"From JSON-LD",'
            '"offers":{"price":"100","priceCurrency":"INR"}}</script>'
            '<meta property="og:title" content="From Meta">'
            '<meta property="product:price:amount" content="999">'
        )
        data = extraction.extract(html, BASE)
        assert data is not None
        assert data.name == "From JSON-LD"
        assert data.price == Decimal("100")

    def test_falls_back_to_meta_when_json_ld_has_no_price(self) -> None:
        html = (
            '<script type="application/ld+json">{"@type":"Product","name":"X"}</script>'
            '<meta property="og:title" content="X">'
            '<meta property="product:price:amount" content="555">'
            '<meta property="product:price:currency" content="INR">'
        )
        data = extraction.extract(html, BASE)
        assert data is not None
        assert data.price == Decimal("555")

    def test_returns_none_for_a_non_product_page(self) -> None:
        assert extraction.extract(load("no_product.html"), BASE) is None

    def test_out_of_stock_without_price_still_extracts(self) -> None:
        """No price, but a real finding about the product -- must not be discarded."""
        data = extraction.extract(load("jsonld_out_of_stock.html"), BASE)
        assert data is not None
        assert data.availability is Availability.OUT_OF_STOCK
