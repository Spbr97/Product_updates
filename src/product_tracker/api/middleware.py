"""Request-level protections.

A body-size cap, applied before FastAPI parses anything. Every request this API accepts is
a small JSON object, so a large body is either a mistake or an attempt to exhaust memory;
either way there is no reason to read it.

``Content-Length`` is checked first because rejecting on the header costs nothing. A
chunked request has no length, so the stream is counted as it arrives and cut off at the
same limit.
"""

from __future__ import annotations

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

#: 413. Spelled numerically: Starlette renames its constants between versions.
_HTTP_413 = 413


def _too_large(limit: int) -> JSONResponse:
    return JSONResponse(
        status_code=_HTTP_413,
        content={
            "error": {
                "type": "payload_too_large",
                "message": f"request body exceeds the {limit} byte limit",
            }
        },
    )


class BodySizeLimitMiddleware:
    """Reject request bodies over ``max_bytes``.

    Written as raw ASGI rather than ``BaseHTTPMiddleware`` so the body can be capped as it
    streams. ``BaseHTTPMiddleware`` buffers the whole request first, which is exactly the
    thing being guarded against.
    """

    def __init__(self, app: ASGIApp, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request = Request(scope)
        declared = request.headers.get("content-length")
        if declared is not None:
            try:
                if int(declared) > self.max_bytes:
                    await _too_large(self.max_bytes)(scope, receive, send)
                    return
            except ValueError:
                # A malformed header is not our problem to diagnose; let the body counter
                # below enforce the limit.
                pass

        received = 0

        async def counting_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_bytes:
                    # Signal end-of-stream; the handler sees a truncated body and fails
                    # validation rather than the server buffering without bound.
                    raise _BodyTooLargeError
            return message

        try:
            await self.app(scope, counting_receive, send)
        except _BodyTooLargeError:
            await _too_large(self.max_bytes)(scope, receive, send)


class _BodyTooLargeError(Exception):
    """Internal signal that the streamed body passed the limit."""

