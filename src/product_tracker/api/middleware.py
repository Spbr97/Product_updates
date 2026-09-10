"""Request-level protections, and the thread that ties a request's logs together.

A body-size cap, applied before FastAPI parses anything. Every request this API accepts is
a small JSON object, so a large body is either a mistake or an attempt to exhaust memory;
either way there is no reason to read it.

``Content-Length`` is checked first because rejecting on the header costs nothing. A
chunked request has no length, so the stream is counted as it arrives and cut off at the
same limit.
"""

from __future__ import annotations

import uuid

import structlog
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

#: Read from the client if present, so a request id set by a proxy or a caller survives
#: into our logs instead of being replaced by one only we know.
REQUEST_ID_HEADER = "x-request-id"

#: A client-supplied id is echoed into logs, so it is bounded and stripped of anything
#: that would break a log line. Untrusted input does not get to shape the output format.
_MAX_REQUEST_ID = 64

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


class RequestIdMiddleware:
    """Give every request an id, and bind it for the life of that request.

    ``configure_logging`` already runs ``merge_contextvars`` first, so anything bound here
    appears on every log line the request produces -- the fetch, the rule match, the
    notification -- without a single call site having to pass it along. That is the whole
    point: correlating a user's report with the lines that explain it, when the log holds
    thousands of interleaved requests.

    Raw ASGI, matching the body-size cap next door. ``BaseHTTPMiddleware`` runs the
    handler in a separate context, which is precisely where contextvars stop propagating.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request_id = _incoming_id(Request(scope)) or uuid.uuid4().hex

        async def send_with_id(message: Message) -> None:
            # Echoed back so a caller reporting a problem can quote the id they saw.
            if message["type"] == "http.response.start":
                headers = message.setdefault("headers", [])
                headers.append((REQUEST_ID_HEADER.encode(), request_id.encode()))
            await send(message)

        structlog.contextvars.bind_contextvars(request_id=request_id)
        try:
            await self.app(scope, receive, send_with_id)
        finally:
            # Workers are reused between requests; leaving the id bound would stamp the
            # next request with the previous one's, which is worse than having none.
            structlog.contextvars.unbind_contextvars("request_id")


def _incoming_id(request: Request) -> str | None:
    """A client's request id, if it sent one worth keeping."""
    supplied = request.headers.get(REQUEST_ID_HEADER)
    if not supplied:
        return None
    cleaned = "".join(c for c in supplied if c.isalnum() or c in "-_.")[:_MAX_REQUEST_ID]
    return cleaned or None
