"""The web layer now serves a built React app, not templates.

The UI's behaviour -- the form, the eight distinct listing states, the per-retailer
comparison -- is covered by the Vitest suite in ``frontend/``. What is left for Python to
check is the serving contract: the bundle is reachable at ``/ui``, hashed assets are
served with the right type, and a deep client route still returns the shell so the router
can take over.

Skips cleanly when the frontend has not been built, so ``pytest`` on a fresh checkout is
still green; CI builds the frontend first.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from product_tracker.api.app import create_app
from product_tracker.web import serve

pytestmark = pytest.mark.db

built = pytest.mark.skipif(
    not serve.is_built(),
    reason="frontend not built (run `npm --prefix frontend run build`)",
)


@pytest.fixture
def client(clean_db: None) -> Iterator[TestClient]:
    with TestClient(create_app(), raise_server_exceptions=False) as c:
        yield c


@built
def test_ui_root_serves_the_shell(client: TestClient) -> None:
    res = client.get("/ui")
    assert res.status_code == 200
    assert "text/html" in res.headers["content-type"]
    assert '<div id="root">' in res.text


@built
def test_a_deep_client_route_returns_the_shell(client: TestClient) -> None:
    """/ui/products/42 is a React route, not a file. It must still get index.html."""
    res = client.get("/ui/products/42")
    assert res.status_code == 200
    assert '<div id="root">' in res.text


@built
def test_the_hashed_bundle_is_referenced_and_served(client: TestClient) -> None:
    import re

    shell = client.get("/ui").text
    asset = re.search(r'/ui/assets/[\w.-]+\.js', shell)
    assert asset, "index.html does not reference a built JS bundle"
    js = client.get(asset.group(0))
    assert js.status_code == 200
    assert "javascript" in js.headers["content-type"]


@built
def test_the_spa_never_shadows_the_api(client: TestClient) -> None:
    """The /ui catch-all must not swallow /api or the health probes."""
    assert client.get("/api/v1/product-entries").status_code in (200, 401)
    assert client.get("/health").status_code == 200


def test_missing_build_is_not_fatal(clean_db: None) -> None:
    """With no bundle, the app still starts and serves the API -- it just has no /ui.
    `mount_ui` early-returns when `is_built()` is false."""
    from unittest.mock import patch

    with (
        patch.object(serve, "is_built", return_value=False),
        TestClient(create_app(), raise_server_exceptions=False) as c,
    ):
        assert c.get("/health").status_code == 200
        assert c.get("/ui").status_code == 404
