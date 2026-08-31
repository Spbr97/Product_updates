from decimal import Decimal

from product_updates.models import Offer
from product_updates.service import detect


def offer(price=100, available=True):
    return Offer("Store", "iPhone 17", "https://example.test/p", Decimal(price), available)


def test_change_detection():
    assert detect(None, offer()).kind == "new"
    assert detect(offer(), offer(99)).kind == "price"
    assert detect(offer(), offer(100, False)).kind == "availability"
    assert detect(offer(), offer()) is None
