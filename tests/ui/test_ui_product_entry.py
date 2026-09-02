"""The Add Product form and the Product Entry page.

Server-rendered HTML, so the existing FastAPI test client is the whole harness -- no
browser driver, no Node. What is asserted is what a person would actually see.

The load-bearing one is :class:`TestHonestStates`. A shop that refused us, a page whose
price would not parse, and a product that is genuinely sold out are three different facts,
and a page that renders them all as "Out of stock" turns a tracker into a rumour mill.
"""

from __future__ import annotations

from collections.abc import Iterator

import httpx
import pytest
import respx
from fastapi.testclient import TestClient
from tests.unit.test_adapters import load

from product_tracker.api.app import create_app

pytestmark = pytest.mark.db

NEW = "/ui/products/new"
SUBMIT = "/ui/products"
AMAZON_URL = "https://www.amazon.in/dp/B0UITEST01"
AMAZON_URL_2 = "https://www.amazon.in/dp/B0UITEST02"
FLIPKART_URL = "https://www.flipkart.com/galaxy-s25/p/itmui000001"
FLIPKART_URL_2 = "https://www.flipkart.com/galaxy-s25/p/itmui000002"


@pytest.fixture(autouse=True)
def _respx_router() -> Iterator[None]:
    with respx.mock:
        yield


@pytest.fixture
def client(clean_db: None) -> Iterator[TestClient]:
    with TestClient(create_app(), raise_server_exceptions=False) as test_client:
        yield test_client


def stub(url: str, fixture: str = "jsonld_in_stock.html") -> None:
    respx.get(url).mock(return_value=httpx.Response(200, html=load(fixture)))


def stub_all() -> None:
    for url in (AMAZON_URL, AMAZON_URL_2, FLIPKART_URL, FLIPKART_URL_2):
        stub(url)


def form(
    name: str = "Samsung Galaxy S25 256GB",
    amazon_url: str = AMAZON_URL,
    flipkart_url: str = FLIPKART_URL,
) -> dict[str, str]:
    return {
        "product_name": name,
        "amazon_name": "S25 on Amazon",
        "amazon_url": amazon_url,
        "flipkart_name": "S25 on Flipkart",
        "flipkart_url": flipkart_url,
    }


def submit(client: TestClient, **kwargs: str) -> httpx.Response:
    return client.post(SUBMIT, data=form(**kwargs), follow_redirects=False)


def create(client: TestClient, **kwargs: str) -> str:
    """Submit the form and return the detail-page path it redirects to."""
    response = submit(client, **kwargs)
    assert response.status_code == 303, response.text[:400]
    return response.headers["location"]


class TestTheForm:
    def test_it_renders(self, client: TestClient) -> None:
        response = client.get(NEW)

        assert response.status_code == 200
        assert "<form" in response.text
        for field in (
            "product_name",
            "amazon_name",
            "amazon_url",
            "flipkart_name",
            "flipkart_url",
        ):
            assert f'name="{field}"' in response.text

    def test_the_stylesheet_is_served(self, client: TestClient) -> None:
        assert client.get("/ui/static/app.css").status_code == 200

    def test_missing_fields_are_refused(self, client: TestClient) -> None:
        response = client.post(SUBMIT, data={"product_name": "only a name"})

        assert response.status_code == 422
        assert "required" in response.text.lower()

    def test_a_rejected_form_keeps_what_was_typed(self, client: TestClient) -> None:
        """Retyping five fields because one was wrong is what stops people using a thing."""
        stub_all()

        response = client.post(SUBMIT, data=form(amazon_url=FLIPKART_URL))

        assert response.status_code == 422
        assert 'value="Samsung Galaxy S25 256GB"' in response.text
        assert f'value="{FLIPKART_URL}"' in response.text

    def test_a_flipkart_link_in_the_amazon_field_is_named(
        self, client: TestClient
    ) -> None:
        stub_all()

        response = client.post(SUBMIT, data=form(amazon_url=FLIPKART_URL))

        assert response.status_code == 422
        assert "Amazon" in response.text

    def test_an_amazon_link_in_the_flipkart_field_is_named(
        self, client: TestClient
    ) -> None:
        stub_all()

        response = client.post(SUBMIT, data=form(flipkart_url=AMAZON_URL))

        assert response.status_code == 422
        assert "Flipkart" in response.text


class TestCreation:
    def test_a_good_form_creates_exactly_one_entry(self, client: TestClient) -> None:
        stub_all()

        create(client)

        listed = client.get("/ui/products")
        assert listed.text.count("Samsung Galaxy S25 256GB") == 1

    def test_it_redirects_to_the_new_entry(self, client: TestClient) -> None:
        stub_all()

        location = create(client)

        assert location.startswith("/ui/products/")
        assert client.get(location).status_code == 200

    def test_submitting_the_same_form_twice_does_not_duplicate(
        self, client: TestClient
    ) -> None:
        """A double-clicked form is a conflict, not a second product."""
        stub_all()
        create(client)

        again = submit(client)

        assert again.status_code == 422
        assert client.get("/ui/products").text.count("Samsung Galaxy S25 256GB") == 1

    def test_reloading_the_detail_page_creates_nothing(
        self, client: TestClient
    ) -> None:
        """303 after POST, so a refresh re-GETs rather than re-posting."""
        stub_all()
        location = create(client)

        for _ in range(3):
            assert client.get(location).status_code == 200

        assert client.get("/ui/products").text.count("Samsung Galaxy S25 256GB") == 1


class TestDetailPage:
    def test_both_retailers_appear_independently(self, client: TestClient) -> None:
        stub_all()

        page = client.get(create(client)).text

        assert "Amazon India" in page
        assert "Flipkart" in page

    def test_the_users_own_names_are_shown(self, client: TestClient) -> None:
        stub_all()

        page = client.get(create(client)).text

        assert "S25 on Amazon" in page
        assert "S25 on Flipkart" in page

    def test_a_fresh_entry_says_not_checked_not_out_of_stock(
        self, client: TestClient
    ) -> None:
        stub_all()

        page = client.get(create(client)).text

        assert "Not checked yet" in page
        assert "Out of stock" not in page

    def test_prices_are_separate_per_retailer(self, client: TestClient) -> None:
        stub_all()
        location = create(client)
        client.post(f"{location}/check", follow_redirects=False)

        page = client.get(location).text

        # Two panels, each with its own price line.
        assert page.count('class="price"') == 2

    def test_the_comparison_table_appears_once_both_are_priced(
        self, client: TestClient
    ) -> None:
        stub_all()
        location = create(client)
        client.post(f"{location}/check", follow_redirects=False)

        page = client.get(location).text

        assert "Price comparison" in page
        assert "Current price" in page

    def test_history_is_kept_per_retailer(self, client: TestClient) -> None:
        stub_all()
        location = create(client)
        client.post(f"{location}/check", follow_redirects=False)

        page = client.get(location).text

        assert "Recent prices" in page
        # A section heading per shop, never one interleaved table.
        assert page.count("<h3>") >= 2


class TestHonestStates:
    """The point of the whole presenter layer."""

    def test_a_refused_shop_does_not_read_as_sold_out(
        self, client: TestClient
    ) -> None:
        stub_all()
        location = create(client)
        respx.get(AMAZON_URL).mock(
            return_value=httpx.Response(403, html="<html>Access Denied</html>")
        )

        client.post(f"{location}/check", follow_redirects=False)
        page = client.get(location).text

        assert "Out of stock" not in page
        assert "Shop refused us" in page or "Check failed" in page

    def test_one_retailer_failing_does_not_hide_the_other(
        self, client: TestClient
    ) -> None:
        stub_all()
        location = create(client)
        respx.get(AMAZON_URL).mock(return_value=httpx.Response(503, text="nope"))

        client.post(f"{location}/check", follow_redirects=False)
        page = client.get(location).text

        # Flipkart's price is still on the page, beside Amazon's failure.
        assert "Flipkart" in page
        assert "₹" in page

    def test_a_paused_entry_says_paused(self, client: TestClient) -> None:
        stub_all()
        location = create(client)

        client.post(f"{location}/pause", follow_redirects=False)
        page = client.get(location).text

        assert "Paused" in page
        assert "Resume" in page


class TestEditing:
    def test_the_edit_form_is_prefilled(self, client: TestClient) -> None:
        stub_all()
        location = create(client)

        page = client.get(f"{location}/edit").text

        assert 'value="Samsung Galaxy S25 256GB"' in page
        assert f'value="{AMAZON_URL}"' in page

    def test_editing_updates_the_same_entry(self, client: TestClient) -> None:
        stub_all()
        location = create(client)
        entry_id = location.rsplit("/", 1)[-1]
        page = client.get(f"{location}/edit").text
        import re

        listing_ids = re.findall(r'name="url_(\d+)"', page)

        response = client.post(
            location,
            data={
                "product_name": "Renamed S25",
                **{f"name_{i}": "kept" for i in listing_ids},
                **{
                    f"url_{i}": url
                    for i, url in zip(listing_ids, [AMAZON_URL, FLIPKART_URL], strict=False)
                },
            },
            follow_redirects=False,
        )

        assert response.status_code == 303
        assert response.headers["location"].endswith(f"/{entry_id}")
        assert "Renamed S25" in client.get(location).text

    def test_changing_a_url_keeps_the_entry_and_its_history(
        self, client: TestClient
    ) -> None:
        stub_all()
        location = create(client)
        entry_id = location.rsplit("/", 1)[-1]
        client.post(f"{location}/check", follow_redirects=False)

        import re

        page = client.get(f"{location}/edit").text
        ids = re.findall(r'name="url_(\d+)"', page)
        amazon_id = ids[0]

        response = client.post(
            location,
            data={
                "product_name": "Samsung Galaxy S25 256GB",
                f"url_{amazon_id}": AMAZON_URL_2,
                f"url_{ids[1]}": FLIPKART_URL,
            },
            follow_redirects=False,
        )

        assert response.status_code == 303
        detail = client.get(f"/ui/products/{entry_id}")
        assert detail.status_code == 200
        assert AMAZON_URL_2 in detail.text
        # The observations recorded at the old URL are still on the page.
        assert "Recent prices" in detail.text


class TestRemoval:
    def test_archiving_moves_it_out_of_the_active_list(
        self, client: TestClient
    ) -> None:
        stub_all()
        location = create(client)

        client.post(f"{location}/archive", follow_redirects=False)

        assert "Samsung Galaxy S25 256GB" not in client.get("/ui/products").text
        archived = client.get("/ui/products?status=archived")
        assert "Samsung Galaxy S25 256GB" in archived.text

    def test_an_unknown_entry_is_404(self, client: TestClient) -> None:
        assert client.get("/ui/products/9999").status_code == 404
