"""Exception handlers producing one consistent error envelope.

Domain exceptions are mapped to status codes here, in one place, so routers can simply
raise and stay free of HTTP concerns. Unexpected exceptions are logged with a traceback
but reported to the client as a bare 500 -- internals are never echoed back.
"""

from __future__ import annotations

from fastapi import FastAPI, Request, status
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from ..core.logging import get_logger
from ..domain.errors import (
    ConfigurationError,
    DuplicateError,
    NoAdapterError,
    NotFoundError,
    ProductTrackerError,
    StoreError,
    ValidationError,
)

log = get_logger(__name__)

# Spelled numerically: Starlette renamed its 422 constant, and the number is stable.
_HTTP_422 = 422

#: Domain exception -> (HTTP status, stable error code).
_ERROR_MAP: tuple[tuple[type[Exception], int, str], ...] = (
    (NotFoundError, status.HTTP_404_NOT_FOUND, "not_found"),
    (DuplicateError, status.HTTP_409_CONFLICT, "conflict"),
    (NoAdapterError, _HTTP_422, "unsupported_store"),
    (ValidationError, _HTTP_422, "validation_error"),
    (ConfigurationError, status.HTTP_500_INTERNAL_SERVER_ERROR, "configuration_error"),
    (StoreError, status.HTTP_502_BAD_GATEWAY, "store_error"),
)


def error_response(
    status_code: int, error_type: str, message: str, detail: dict | None = None
) -> JSONResponse:
    """Build the standard error envelope.

    ``detail`` goes through ``jsonable_encoder`` because pydantic validation errors carry
    the offending constraint in ``ctx`` -- and a constraint on a Decimal field is a
    ``Decimal``, which ``json.dumps`` cannot serialise. Encoding it here means no handler
    can turn a 422 into a 500 by reporting the reason.
    """
    body: dict[str, object] = {"type": error_type, "message": message}
    if detail is not None:
        body["detail"] = jsonable_encoder(detail)
    return JSONResponse(status_code=status_code, content={"error": body})


def _classify(exc: Exception) -> tuple[int, str]:
    for exc_type, status_code, error_type in _ERROR_MAP:
        if isinstance(exc, exc_type):
            return status_code, error_type
    return status.HTTP_500_INTERNAL_SERVER_ERROR, "internal_error"


def register_exception_handlers(app: FastAPI) -> None:
    """Attach handlers so every failure leaves through the same envelope."""

    @app.exception_handler(ProductTrackerError)
    async def _domain_error(_request: Request, exc: ProductTrackerError) -> JSONResponse:
        status_code, error_type = _classify(exc)
        if status_code >= 500:
            log.error("api.error", error_type=error_type, exc_info=exc)
            return error_response(status_code, error_type, "Internal server error")
        return error_response(status_code, error_type, str(exc))

    @app.exception_handler(RequestValidationError)
    async def _request_validation(
        _request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        return error_response(
            _HTTP_422,
            "validation_error",
            "Request validation failed",
            {"errors": exc.errors()},
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http_exception(_request: Request, exc: StarletteHTTPException) -> JSONResponse:
        return error_response(
            exc.status_code,
            _HTTP_ERROR_TYPES.get(exc.status_code, "http_error"),
            str(exc.detail),
        )

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        log.error("api.unhandled", path=request.url.path, exc_info=exc)
        return error_response(
            status.HTTP_500_INTERNAL_SERVER_ERROR, "internal_error", "Internal server error"
        )


_HTTP_ERROR_TYPES: dict[int, str] = {
    400: "bad_request",
    401: "unauthorized",
    403: "forbidden",
    404: "not_found",
    405: "method_not_allowed",
    409: "conflict",
    413: "payload_too_large",
    422: "validation_error",
    429: "rate_limited",
    503: "unavailable",
}
