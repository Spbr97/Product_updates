"""The Product Entry endpoints over HTTP.

Service behaviour is covered in ``test_product_entries.py``. What matters here is the HTTP
contract: the status codes, the error envelope, that a retailer failing is reported rather
than turned into a 500, and that one account cannot reach another's entries.
"""

from __future__ import annotations

from collections.abc import Iterator

import httpx
import pytest
import respx
from fastapi.testclient import TestClient
from tests.unit.test_adapters import load

from product_tracker.api.app import create_app
from product_tracker.api.deps import API_KEY_HEADER

pytestmark = pytest.mark.db

ENTRIES = "/api/v1/product-entries"
AMAZON_URL = "https://www.amazon.in/dp/B0APITEST1"
AMAZON_URL_2 = "https://www.amazon.in/dp/B0APITEST2"
FLIPKART_URL = "https://www.flipkart.com/galaxy-s25/p/itmapi0001"
FLIPKART_URL_2 = "https://www.flipkart.com/galaxy-s25/p/itmapi0002"


@pytest.fixture(autouse=True)
def _respx_router() -> Iterator[None]:
    with respx.mock:
        yield


@pytest.fixture
def client(clean_db: None) -> Iterator[TestClient]:
    with TestClient(create_app(), raise_server_exceptions=False) as test_client:
        yield test_client


def stub(url: str, fixture: str = "jsonld_in_stock.html", status: int = 200) -> None:
    respx.get(url).mock(return_value=httpx.Response(status, html=load(fixture)))


def stub_all() -> None:
    for url in (AMAZON_URL, AMAZON_URL_2, FLIPKART_URL, FLIPKART_URL_2):
        stub(url)


def payload(
    name: str = "Samsung Galaxy S25 256GB",
    amazon_url: str = AMAZON_URL,
    flipkart_url: str = FLIPKART_URL,
) -> dict[str, object]:
    return {
        "product_name": name,
        "amazon": {"product_name": "S25 on Amazon", "url": amazon_url},
        "flipkart": {"product_name": "S25 on Flipkart", "url": flipkart_url},
    }


def create(client: TestClient, **kwargs: object) -> dict:
    response = client.post(ENTRIES, json=payload(**kwargs))  # type: ignore[arg-type]
    assert response.status_code == 201, response.text
    return response.json()


class TestCreate:
    def test_creates_one_entry_with_two_listings(self, client: TestClient) -> None:
        stub_all()

        body = create(client)

        assert body["status"] == "active"
        assert sorted(x["store"] for x in body["listings"]) == ["amazon-in", "flipkart"]
        assert body["product_name"] == "Samsung Galaxy S25 256GB"

    def test_it_returns_before_the_shops_are_read(self, client: TestClient) -> None:
        """A form submission must not wait on two retailers. Prices arrive with the first
        background check, so they are null here -- and null, not zero, and not a guess."""
        stub_all()

        body = create(client)

        assert all(x["price"] is None for x in body["listings"])
        assert all(x["availability"] == "unknown" for x in body["listings"])

    def test_the_store_display_name_is_included(self, client: TestClient) -> None:
        """So a client does not have to own the slug-to-name mapping."""
        stub_all()

        names = {x["store"]: x["store_name"] for x in create(client)["listings"]}

        assert names == {"amazon-in": "Amazon India", "flipkart": "Flipkart"}

    def test_a_missing_field_is_422(self, client: TestClient) -> None:
        response = client.post(ENTRIES, json={"product_name": "only a name"})
        assert response.status_code == 422

    def test_an_empty_name_is_422(self, client: TestClient) -> None:
        stub_all()
        response = client.post(ENTRIES, json=payload(name=""))
        assert response.status_code == 422

    def test_a_wrong_retailer_url_is_422_and_named(self, client: TestClient) -> None:
        stub_all()

        response = client.post(ENTRIES, json=payload(amazon_url=FLIPKART_URL))

        assert response.status_code == 422
        error = response.json()["error"]
        assert error["type"] == "invalid_store_url"
        assert "Amazon" in error["message"]

    def test_a_duplicate_url_is_409_naming_the_entry(self, client: TestClient) -> None:
        stub_all()
        first = create(client)

        response = client.post(
            ENTRIES, json=payload(name="again", flipkart_url=FLIPKART_URL_2)
        )

        assert response.status_code == 409
        error = response.json()["error"]
        assert error["type"] == "duplicate_listing"
        assert str(first["id"]) in error["message"]

    def test_submitting_twice_does_not_create_two_entries(
        self, client: TestClient
    ) -> None:
        """A double-clicked form is a conflict, not a second product."""
        stub_all()
        create(client)

        second = client.post(ENTRIES, json=payload())

        assert second.status_code == 409
        assert client.get(ENTRIES).json()["total"] == 1


class TestReadAndList:
    def test_get_returns_per_retailer_state(self, client: TestClient) -> None:
        stub_all()
        entry = create(client)

        body = client.get(f"{ENTRIES}/{entry['id']}").json()

        assert len(body["listings"]) == 2
        for listing in body["listings"]:
            assert {"price", "currency", "availability", "last_check_status"} <= set(listing)

    def test_listing_is_paginated(self, client: TestClient) -> None:
        stub_all()
        create(client, name="First")
        create(client, name="Second", amazon_url=AMAZON_URL_2, flipkart_url=FLIPKART_URL_2)

        body = client.get(ENTRIES, params={"limit": 1}).json()

        assert body["total"] == 2
        assert len(body["items"]) == 1

    def test_archived_can_be_filtered(self, client: TestClient) -> None:
        stub_all()
        create(client, name="Kept")
        gone = create(
            client, name="Gone", amazon_url=AMAZON_URL_2, flipkart_url=FLIPKART_URL_2
        )
        client.delete(f"{ENTRIES}/{gone['id']}")

        active = client.get(ENTRIES, params={"status": "active"}).json()

        assert [x["product_name"] for x in active["items"]] == ["Kept"]

    def test_a_missing_entry_is_404(self, client: TestClient) -> None:
        assert client.get(f"{ENTRIES}/9999").status_code == 404


class TestUpdate:
    def test_renaming_keeps_the_id(self, client: TestClient) -> None:
        stub_all()
        entry = create(client)

        response = client.patch(
            f"{ENTRIES}/{entry['id']}", json={"canonical_name": "Renamed"}
        )

        assert response.status_code == 200
        assert response.json()["id"] == entry["id"]
        assert response.json()["product_name"] == "Renamed"

    def test_changing_a_listing_url_keeps_both_ids(self, client: TestClient) -> None:
        stub_all()
        entry = create(client)
        amazon = next(x for x in entry["listings"] if x["store"] == "amazon-in")

        response = client.patch(
            f"{ENTRIES}/{entry['id']}/listings/{amazon['id']}",
            json={"url": AMAZON_URL_2},
        )

        assert response.status_code == 200
        assert response.json()["id"] == amazon["id"]
        assert response.json()["url"] == AMAZON_URL_2
        assert client.get(f"{ENTRIES}/{entry['id']}").json()["id"] == entry["id"]

    def test_a_wrong_retailer_url_on_update_is_422(self, client: TestClient) -> None:
        stub_all()
        entry = create(client)
        amazon = next(x for x in entry["listings"] if x["store"] == "amazon-in")

        response = client.patch(
            f"{ENTRIES}/{entry['id']}/listings/{amazon['id']}",
            json={"url": FLIPKART_URL_2},
        )

        assert response.status_code == 422
        assert response.json()["error"]["type"] == "invalid_store_url"


class TestRemoval:
    def test_removing_a_listing_leaves_the_other(self, client: TestClient) -> None:
        stub_all()
        entry = create(client)
        amazon = next(x for x in entry["listings"] if x["store"] == "amazon-in")

        assert (
            client.delete(
                f"{ENTRIES}/{entry['id']}/listings/{amazon['id']}"
            ).status_code
            == 204
        )

        body = client.get(f"{ENTRIES}/{entry['id']}").json()
        active = [x for x in body["listings"] if x["is_active"]]
        assert [x["store"] for x in active] == ["flipkart"]

    def test_a_removed_listing_is_still_visible_as_inactive(
        self, client: TestClient
    ) -> None:
        """Its observations are still there; hiding the row would hide the history."""
        stub_all()
        entry = create(client)
        amazon = next(x for x in entry["listings"] if x["store"] == "amazon-in")
        client.delete(f"{ENTRIES}/{entry['id']}/listings/{amazon['id']}")

        body = client.get(f"{ENTRIES}/{entry['id']}").json()

        removed = next(x for x in body["listings"] if x["store"] == "amazon-in")
        assert removed["is_active"] is False
        assert removed["deactivated_at"] is not None

    def test_archiving_is_204_and_soft(self, client: TestClient) -> None:
        stub_all()
        entry = create(client)

        assert client.delete(f"{ENTRIES}/{entry['id']}").status_code == 204

        body = client.get(f"{ENTRIES}/{entry['id']}").json()
        assert body["status"] == "archived"
        assert body["deleted_at"] is not None


class TestChecking:
    def test_check_reports_each_retailer_separately(self, client: TestClient) -> None:
        stub_all()
        entry = create(client)

        response = client.post(f"{ENTRIES}/{entry['id']}/check")

        assert response.status_code == 200
        results = response.json()["results"]
        assert sorted(x["store"] for x in results) == ["amazon-in", "flipkart"]

    def test_one_retailer_failing_does_not_hide_the_other(
        self, client: TestClient
    ) -> None:
        """The point of retailer isolation, asserted rather than assumed."""
        stub_all()
        entry = create(client)
        respx.get(AMAZON_URL).mock(return_value=httpx.Response(503, text="nope"))

        results = client.post(f"{ENTRIES}/{entry['id']}/check").json()["results"]

        amazon = next(x for x in results if x["store"] == "amazon-in")
        flipkart = next(x for x in results if x["store"] == "flipkart")
        assert amazon["status"] != "success"
        assert flipkart["status"] == "success"
        assert flipkart["price"] is not None

    def test_every_retailer_failing_is_still_200(self, client: TestClient) -> None:
        """A recorded failure is a successfully observed fact. A 5xx would claim this API
        broke when it did not."""
        stub_all()
        entry = create(client)
        for url in (AMAZON_URL, FLIPKART_URL):
            respx.get(url).mock(return_value=httpx.Response(503, text="nope"))

        response = client.post(f"{ENTRIES}/{entry['id']}/check")

        assert response.status_code == 200
        assert all(x["status"] != "success" for x in response.json()["results"])

    def test_a_failure_never_reads_as_out_of_stock(self, client: TestClient) -> None:
        stub_all()
        entry = create(client)
        respx.get(AMAZON_URL).mock(return_value=httpx.Response(503, text="nope"))

        results = client.post(f"{ENTRIES}/{entry['id']}/check").json()["results"]

        amazon = next(x for x in results if x["store"] == "amazon-in")
        assert amazon["availability"] in (None, "unknown")

    def test_one_listing_can_be_checked_alone(self, client: TestClient) -> None:
        stub_all()
        entry = create(client)
        amazon = next(x for x in entry["listings"] if x["store"] == "amazon-in")

        results = client.post(
            f"{ENTRIES}/{entry['id']}/listings/{amazon['id']}/check"
        ).json()["results"]

        assert [x["store"] for x in results] == ["amazon-in"]


class TestHistoryAndStats:
    def test_history_is_split_by_retailer(self, client: TestClient) -> None:
        stub_all()
        entry = create(client)
        client.post(f"{ENTRIES}/{entry['id']}/check")

        body = client.get(f"{ENTRIES}/{entry['id']}/history").json()

        assert sorted(x["store"] for x in body["listings"]) == ["amazon-in", "flipkart"]
        for section in body["listings"]:
            assert "prices" in section and "availability" in section

    def test_stats_are_per_retailer(self, client: TestClient) -> None:
        stub_all()
        entry = create(client)
        client.post(f"{ENTRIES}/{entry['id']}/check")

        body = client.get(f"{ENTRIES}/{entry['id']}/stats").json()

        assert len(body["listings"]) == 2
        for row in body["listings"]:
            assert row["mixed_currency"] is False


class TestPauseResume:
    def test_pause_then_resume(self, client: TestClient) -> None:
        stub_all()
        entry = create(client)

        paused = client.post(f"{ENTRIES}/{entry['id']}/pause").json()
        assert all(x["tracking_status"] == "paused" for x in paused["listings"])

        resumed = client.post(f"{ENTRIES}/{entry['id']}/resume").json()
        assert all(x["tracking_status"] == "active" for x in resumed["listings"])


class TestOwnershipOverHttp:
    KEY_A = "key-for-alice"

    def test_another_account_gets_404_not_403(self, client: TestClient) -> None:
        """403 would confirm the id exists, and ids are sequential."""
        from product_tracker.db.session import session_scope
        from product_tracker.services import user_service

        stub_all()
        with session_scope() as session:
            alice = user_service.create_user(session, email="alice@example.com").api_key
            bob = user_service.create_user(session, email="bob@example.com").api_key

        made = client.post(
            ENTRIES, json=payload(), headers={API_KEY_HEADER: alice}
        )
        assert made.status_code == 201, made.text
        entry_id = made.json()["id"]

        assert (
            client.get(
                f"{ENTRIES}/{entry_id}", headers={API_KEY_HEADER: bob}
            ).status_code
            == 404
        )
        assert (
            client.delete(
                f"{ENTRIES}/{entry_id}", headers={API_KEY_HEADER: bob}
            ).status_code
            == 404
        )
        assert (
            client.post(
                f"{ENTRIES}/{entry_id}/check", headers={API_KEY_HEADER: bob}
            ).status_code
            == 404
        )


class TestProtection:
    def test_an_oversized_body_is_413(self, client: TestClient) -> None:
        """The body-size guard applies to every mutating route, this one included."""
        fat = {**payload(), "product_name": "x" * 200_000}

        response = client.post(ENTRIES, json=fat)

        assert response.status_code == 413

class TestTrackingMatrix:
    """The §56 tracking rows, at the entry level, through /check and /history.

    Each check runs against a listing's product through the ordinary engine; what is
    asserted is that the entry does not get in the way -- one shop's result never lands in
    another shop's history, an unchanged price adds nothing, and a missing price is
    recorded without becoming "out of stock".
    """

    def _amazon_prices(self, client: TestClient, entry_id: int) -> list:
        body = client.get(f"{ENTRIES}/{entry_id}/history").json()
        section = next(s for s in body["listings"] if s["store"] == "amazon-in")
        return [p["price"] for p in section["prices"]]

    def _flipkart_prices(self, client: TestClient, entry_id: int) -> list:
        body = client.get(f"{ENTRIES}/{entry_id}/history").json()
        section = next(s for s in body["listings"] if s["store"] == "flipkart")
        return [p["price"] for p in section["prices"]]

    def test_a_price_change_on_amazon_leaves_flipkart_untouched(
        self, client: TestClient
    ) -> None:
        stub_all()
        entry = create(client)
        eid = entry["id"]
        client.post(f"{ENTRIES}/{eid}/check")
        flipkart_before = self._flipkart_prices(client, eid)

        # Amazon now quotes a different page; Flipkart's is unchanged.
        respx.get(AMAZON_URL).mock(
            return_value=httpx.Response(200, html=load("jsonld_out_of_stock.html"))
        )
        client.post(f"{ENTRIES}/{eid}/check")

        assert self._flipkart_prices(client, eid) == flipkart_before

    def test_an_unchanged_price_adds_no_history_row(self, client: TestClient) -> None:
        """Flipkart's page (JSON-LD) is the one the fixture parses; three identical reads
        must still leave exactly one observation."""
        stub_all()
        eid = create(client)["id"]

        client.post(f"{ENTRIES}/{eid}/check")
        client.post(f"{ENTRIES}/{eid}/check")
        client.post(f"{ENTRIES}/{eid}/check")

        assert len(self._flipkart_prices(client, eid)) == 1

    def test_a_missing_price_is_recorded_and_is_not_out_of_stock(
        self, client: TestClient
    ) -> None:
        stub_all()
        entry = create(client)
        eid = entry["id"]
        respx.get(AMAZON_URL).mock(
            return_value=httpx.Response(200, html=load("amazon_no_price.html"))
        )

        results = client.post(f"{ENTRIES}/{eid}/check").json()["results"]
        amazon = next(r for r in results if r["store"] == "amazon-in")

        assert amazon["status"] != "success"
        assert amazon["price"] is None
        assert amazon["availability"] in (None, "unknown")
        # And the listing on the detail response still shows no price, not a zero.
        listing = next(
            x
            for x in client.get(f"{ENTRIES}/{eid}").json()["listings"]
            if x["store"] == "amazon-in"
        )
        assert listing["price"] is None

