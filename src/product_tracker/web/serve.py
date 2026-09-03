"""Serve the built React app at /ui.

The bundle is committed nowhere -- it is produced by ``npm run build`` in ``frontend/``
and lands in ``web/app/``. When it is absent (a checkout that has not built the frontend),
the routes simply are not mounted and a one-line warning says why, so the API still starts
and every non-UI test still runs.

Client-side routes like ``/ui/products/42`` do not correspond to a file, so any path under
``/ui`` that is not a built asset falls through to ``index.html`` and the router takes it
from there.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from ..core.logging import get_logger

log = get_logger(__name__)

APP_DIR = Path(__file__).parent / "app"
INDEX = APP_DIR / "index.html"


def is_built() -> bool:
    return INDEX.is_file()


def mount_ui(app: FastAPI) -> None:
    """Attach the SPA to ``app`` at ``/ui``. A no-op when the frontend is not built."""
    if not is_built():
        log.warning(
            "web.ui_not_built",
            hint="run `npm --prefix frontend ci && npm --prefix frontend run build`",
        )
        return

    # Hashed, immutable bundles: let the browser cache them hard.
    app.mount(
        "/ui/assets",
        StaticFiles(directory=APP_DIR / "assets"),
        name="ui-assets",
    )

    @app.get("/ui", include_in_schema=False)
    @app.get("/ui/{_path:path}", include_in_schema=False)
    def spa(_path: str = "") -> FileResponse:
        # Every non-asset path under /ui is a client route; the shell decides what to show.
        return FileResponse(INDEX)

    log.info("web.ui_mounted", path="/ui")
