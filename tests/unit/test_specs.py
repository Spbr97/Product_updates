"""Reading a product's specifications, by category.

What separates two models depends on what they are. Reading storage and colour from
everything -- which this project did until now -- gives three identical rows for three
different earbuds, and the comparison is useless while looking like it works.
"""

from __future__ import annotations

import pytest

from product_tracker.services.specs import (
    GENERIC,
    available_profiles,
    detect_category,
    display_fields,
    label_fields,
    load_profile,
    read_specs,
    render_specs,
)

EARBUDS = "boAt Airdopes 311 Pro, 50 Hours Playtime, 13mm Drivers, ANC - Black"
POWERBANK = "Anker 737 Power Bank 24000mAh 140W Fast Charging - Black"
PHONE = "Samsung Galaxy S25 5G (12GB RAM, 256GB Storage) Navy"


class TestDetection:
    @pytest.mark.parametrize(
        ("title", "expected"),
        [
            (EARBUDS, "earbuds"),
            (POWERBANK, "powerbank"),
            (PHONE, "phone"),
            ("Sony WH-1000XM5 Wireless Headphones 30 Hours", "earbuds"),
            ("Ambrane 20,000 mAh Power Bank 22.5W", "powerbank"),
            ("A Wooden Chopping Board, Large", GENERIC),
            (None, GENERIC),
        ],
    )
    def test_reads_the_kind_from_the_title(self, title: str | None, expected: str) -> None:
        assert detect_category(title) == expected

    def test_a_compatibility_list_does_not_decide_the_category(self) -> None:
        """The bug this scoring exists for.

        A power bank's title lists what it charges -- "Supports Android, Apple, Tablets,
        Earbuds, Watch" -- and first-match-wins made it an earbuds listing, whose profile
        has no idea what to do with an mAh capacity.
        """
        title = (
            "Xiaomi Power Bank 4i 20000mAh 33W Super Fast Charging PD | Type C | "
            "Supports Android, Apple, Tablets, Earbuds, Watch (MI Powerbank), Black"
        )
        assert detect_category(title) == "powerbank"

    def test_earbuds_mentioning_a_power_bank_are_still_earbuds(self) -> None:
        """The mirror case, which an ordering tweak alone would have broken."""
        title = "boAt Airdopes with power bank style case, 40H playback, 13mm drivers"
        assert detect_category(title) == "earbuds"


class TestReadingFields:
    def test_earbuds(self) -> None:
        category, attributes = read_specs(EARBUDS)

        assert category == "earbuds"
        assert attributes["playtime"] == "50h"
        assert attributes["driver"] == "13mm"
        assert attributes["anc"] == "ANC"
        assert attributes["colour"] == "Black"

    def test_powerbank(self) -> None:
        category, attributes = read_specs(POWERBANK)

        assert category == "powerbank"
        assert attributes["capacity"] == "24000mAh"
        assert attributes["output"] == "140W"

    def test_a_capacity_written_with_a_comma(self) -> None:
        """Shops write both "20000mAh" and "20,000 mAh"."""
        _category, attributes = read_specs("Ambrane 20,000 mAh Power Bank 22.5W - Blue")
        assert attributes["capacity"] == "20000mAh"

    def test_phone_storage_and_ram_are_told_apart(self) -> None:
        """The regression that predates categories and must survive them.

        Each "RAM" token claims the single *nearest* capacity. A proximity window sits
        close to both numbers in "8GB RAM, 256GB Storage" and discards the storage figure
        too, so the title yields nothing at all.
        """
        _category, attributes = read_specs(PHONE)

        assert attributes["storage"] == "256GB"
        assert attributes["ram"] == "12GB"

    @pytest.mark.parametrize(
        ("title", "storage"),
        [
            ("Galaxy S25 (8GB RAM, 256GB Storage)", "256GB"),
            ("Galaxy S25 (12 GB RAM | 512 GB ROM)", "512GB"),
            ("Redmi Note 14 6GB RAM 128GB", "128GB"),
        ],
    )
    def test_memory_is_never_mistaken_for_storage(self, title: str, storage: str) -> None:
        _category, attributes = read_specs(title, "phone")
        assert attributes["storage"] == storage

    def test_an_unknown_product_yields_colour_only(self) -> None:
        """Pulling every number out of an unfamiliar title is confident nonsense.

        An absent specification is honest; a wrong one is worse than none.
        """
        category, attributes = read_specs("A Wooden Chopping Board, Large - Teal")

        assert category == GENERIC
        assert attributes == {"colour": "Teal"}

    def test_nothing_readable_is_an_empty_result(self) -> None:
        _category, attributes = read_specs("Mystery Item 9000")
        assert attributes == {}

    def test_a_field_the_title_omits_is_simply_absent(self) -> None:
        """No placeholders. A missing driver size is missing, not "unknown"."""
        _category, attributes = read_specs("boAt Airdopes 311, 50H Playtime - Blue")
        assert "driver" not in attributes
        assert attributes["playtime"] == "50h"


class TestRendering:
    def test_specs_render_in_the_profile_order(self) -> None:
        category, attributes = read_specs(EARBUDS)
        assert render_specs(category, attributes) == "50h · 13mm · ANC"

    def test_only_the_fields_worth_showing_appear(self) -> None:
        """Colour names the model and is already the row label; repeating it is noise."""
        category, attributes = read_specs(EARBUDS)
        assert "Black" not in render_specs(category, attributes)

    def test_an_unknown_category_renders_nothing(self) -> None:
        category, attributes = read_specs("A Wooden Chopping Board - Teal")
        assert render_specs(category, attributes) == ""

    def test_no_attributes_renders_nothing(self) -> None:
        assert render_specs("earbuds", {}) == ""


class TestProfiles:
    def test_every_shipped_profile_loads(self) -> None:
        """A malformed profile should fail here, not silently read nothing in production."""
        for name in (*available_profiles(), GENERIC):
            profile = load_profile(name)
            assert profile.category
            assert profile.fields

    def test_naming_fields_are_a_subset_of_the_fields_read(self) -> None:
        """A label field nobody reads produces variants that never get a name."""
        for name in (*available_profiles(), GENERIC):
            profile = load_profile(name)
            known = {spec.name for spec in profile.fields}
            assert set(profile.label) <= known, name
            assert set(profile.display) <= known, name

    def test_phones_are_named_by_storage_and_colour(self) -> None:
        assert label_fields("phone") == ("storage", "colour")

    def test_earbuds_show_what_actually_differs(self) -> None:
        assert display_fields("earbuds") == ("playtime", "driver", "anc")
