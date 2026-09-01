"""FastAPI application factory.

``create_app`` builds a fully wired application. Configuration is read once at startup and
fails fast: a process that cannot read its settings should not accept traffic.

Versioned routes live under ``/api/v1``. The health endpoints are intentionally
unversioned -- probes should not have to change when the API version does.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import APIRouter, FastAPI

from .. import __version__
from ..core.config import Settings, get_settings
from ..core.logging import configure_logging, get_logger
from ..db.session import get_engine
from .deps import RequireRead
from .errors import register_exception_handlers
from .middleware import BodySizeLimitMiddleware
from .schemas.common import ErrorResponse

log = get_logger(__name__)

API_V1_PREFIX = "/api/v1"

DESCRIPTION = """
Track product prices and availability across e-commerce sites.

Add a product by URL, and the platform identifies the store, extracts price and stock,
records history, evaluates alert rules, and notifies through configured providers.
""".strip()


def build_v1_router() -> APIRouter:
    """Assemble the versioned router.

    Every versioned resource is mounted here.
    """
    from .routers import alerts, history, products, stores

    # The read guard is attached once, here: every versioned route inherits it, so a new
    # router cannot be added and accidentally left unguarded. Write endpoints add their
    # own stricter dependency. Health is mounted outside this router and stays open.
    router = APIRouter(prefix=API_V1_PREFIX, dependencies=[RequireRead])
    router.include_router(products.router)
    router.include_router(history.router)
    router.include_router(alerts.router)
    router.include_router(stores.router)
    return router


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    log.info("api.startup", version=__version__)
    try:
        yield
    finally:
        # Return pooled connections before the process exits so Postgres does not keep
        # them until its own timeout.
        get_engine().dispose()
        log.info("api.shutdown")


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(level=settings.log_level, fmt=settings.log_format)

    app = FastAPI(
        title="Product Tracker",
        description=DESCRIPTION,
        version=__version__,
        lifespan=lifespan,
        docs_url="/docs",
        redoc_url="/redoc",
        openapi_url="/openapi.json",
        responses={
            401: {"model": ErrorResponse, "description": "Missing or invalid API key"},
            413: {"model": ErrorResponse, "description": "Request body too large"},
            422: {"model": ErrorResponse, "description": "Validation error"},
            500: {"model": ErrorResponse, "description": "Internal error"},
        },
    )

    register_exception_handlers(app)

    # Applied before routing: every request this API accepts is a small JSON object, so a
    # large body is either a mistake or an attempt to exhaust memory.
    app.add_middleware(
        BodySizeLimitMiddleware, max_bytes=settings.api_max_request_bytes
    )

    from .routers import health  # Imported here to keep module import side-effect free.

    app.include_router(health.router)
    app.include_router(build_v1_router())

    return app


# Module-level app for `uvicorn product_tracker.api.app:app`.
def get_app() -> FastAPI:
    return create_app()
