"""Reading a model and colour out of a listing title.

The behaviour that matters most is the first test: three shops spell the same phone three
different ways, and all three must resolve to one variant. If they do not, the comparison
grid silently shows one model as three, each priced at one shop, and the whole feature is
worthless while looking like it works.
"""

from __future__ import annotations

import pytest

from product_tracker.services.variants import (
    infer_colour,
    infer_storage,
    infer_variant,
    infer_variant_from_url,
    sort_position,
    variant_label,
)


class TestCrossStoreAgreement:
    """The same phone, as each shop writes it."""

    @pytest.mark.parametrize(
        "title",
        [
            "Apple iPhone 17 (Black, 256 GB)",  # Flipkart
            "Apple iPhone 17 256 GB, Black",  # Reliance Digital
            "APPLE iPhone 17 (256GB Storage, Black)",  # Vijay Sales
            "Apple iPhone 17 (256GB, Black), 1 Unit",  # BigBasket
            "apple iphone 17 256gb black",  # a slug-ish title
        ],
    )
    def test_every_spelling_resolves_to_one_variant(self, title: str) -> None:
        assert infer_variant(title) == ("256GB / Black", {"storage": "256GB", "colour": "Black"})


class TestStorage:
    @pytest.mark.parametrize(
        ("title", "expected"),
        [
            ("iPhone 17 (256 GB)", "256GB"),
            ("iPhone 17 256GB", "256GB"),
            ("iPhone 17 Pro Max 1 TB", "1TB"),
            ("iPhone 17 1TB", "1TB"),
        ],
    )
    def test_reads_capacity_however_it_is_spaced(self, title: str, expected: str) -> None:
        assert infer_storage(title) == expected

    @pytest.mark.parametrize(
        ("title", "expected"),
        [
            ("Galaxy S25 (8GB RAM, 256GB Storage)", "256GB"),
            ("Galaxy S25 (12 GB RAM | 512 GB ROM)", "512GB"),
            ("Redmi Note 14 6GB RAM 128GB", "128GB"),
            ("Laptop 16GB RAM 1TB SSD", "1TB"),
        ],
    )
    def test_memory_is_not_mistaken_for_storage(self, title: str, expected: str) -> None:
        """Regression: a proximity window let "RAM" disqualify *both* capacities.

        In "8GB RAM, 256GB Storage" the token sits near each number, so a window-based
        rule discarded the storage figure too and the title yielded nothing at all.
        """
        assert infer_storage(title) == expected

    def test_bare_numbers_are_not_capacities(self) -> None:
        # A model number must never be read as a size.
        assert infer_storage("Sony WH-1000XM5 Headphones") is None
        assert infer_storage("Product /p/317396") is None


class TestColour:
    @pytest.mark.parametrize(
        ("title", "expected"),
        [
            ("iPhone 17 - Lavender", "Lavender"),
            ("iPhone 17 (Sage)", "Sage"),
            ("iPhone 17 Pro - Desert Titanium", "Desert Titanium"),
            ("Redmi Note 14 Midnight Black", "Midnight Black"),
        ],
    )
    def test_reads_colour(self, title: str, expected: str) -> None:
        assert infer_colour(title) == expected

    def test_multiword_colours_win_over_their_suffix(self) -> None:
        """"Midnight Black" must not be read as plain "Black"."""
        assert infer_colour("Phone in Midnight Black") == "Midnight Black"
        assert infer_colour("Watch in Rose Gold") == "Rose Gold"

    def test_unknown_colour_is_absent_not_guessed(self) -> None:
        assert infer_colour("iPhone 17 - Vermillion Sparkle") is None


class TestRefusalToGuess:
    """A wrong grouping merges two different products, so silence is the safer failure."""

    def test_no_clues_yields_nothing(self) -> None:
        assert infer_variant("Some Product With No Clues") == (None, {})

    def test_missing_title_yields_nothing(self) -> None:
        assert infer_variant(None) == (None, {})
        assert infer_variant("") == (None, {})


class TestUrlFallback:
    """What we are left with when a shop blocks us and the listing has no title at all."""

    def test_reads_a_variant_out_of_a_slug(self) -> None:
        label, attrs = infer_variant_from_url(
            "https://www.croma.com/apple-iphone-17-256gb-black-/p/317396"
        )
        assert label == "256GB / Black"
        assert attrs == {"storage": "256GB", "colour": "Black"}

    def test_trailing_id_segments_are_not_capacities(self) -> None:
        # "/p/317396" must not become a storage size.
        _label, attrs = infer_variant_from_url("https://shop.example/thing-blue/p/512000")
        assert "storage" not in attrs

    def test_no_url_yields_nothing(self) -> None:
        assert infer_variant_from_url(None) == (None, {})


class TestLabelStability:
    def test_label_ordering_does_not_depend_on_dict_order(self) -> None:
        """The label carries a uniqueness constraint, so it must be deterministic."""
        one = variant_label({"colour": "Black", "storage": "256GB"})
        two = variant_label({"storage": "256GB", "colour": "Black"})
        assert one == two == "256GB / Black"

    def test_extra_attributes_sort_alphabetically(self) -> None:
        label = variant_label({"storage": "256GB", "colour": "Black", "network": "5G"})
        assert label == "256GB / Black / 5G"


class TestSortPosition:
    def test_capacities_order_by_size_not_alphabetically(self) -> None:
        """Alphabetically "1TB" falls between "128GB" and "256GB", which reads as a bug."""
        sizes = ["128GB", "256GB", "512GB", "1TB"]
        positions = [sort_position({"storage": size}) for size in sizes]
        assert positions == sorted(positions)

    def test_missing_storage_sorts_first(self) -> None:
        assert sort_position({"colour": "Black"}) == 0
