from decimal import Decimal

from product_updates.models import Offer
from product_updates.variants import group_candidates


def test_same_price_colours_are_grouped():
    offers = [
        Offer("Store", "Phone 256 GB Black", "https://example.test/black", Decimal("100"), True),
        Offer("Store", "Phone 256 GB White", "https://example.test/white", Decimal("100"), True),
    ]
    grouped = group_candidates(offers)
    assert len(grouped) == 1
    assert grouped[0].colours == ("Black", "White")


def test_different_colour_price_stays_separate():
    offers = [
        Offer("Store", "Phone Black", "https://example.test/black", Decimal("100"), True),
        Offer("Store", "Phone White", "https://example.test/white", Decimal("99"), True),
    ]
    assert len(group_candidates(offers)) == 2
