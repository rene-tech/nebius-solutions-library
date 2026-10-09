"""Transparent HTTP access logging, including disconnected ASGI requests."""

import asyncio
import json
import logging
import time

from starlette.types import ASGIApp, Message, Receive, Scope, Send

LOGGER = logging.getLogger("fs2_serve.access")


class AccessLogMiddleware:
    """Observe ASGI messages without a response-repackaging task or body drain."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        started = time.monotonic()
        response_started: float | None = None
        status: int | None = None
        complete = disconnected = False
        error_type: str | None = None

        async def observed_receive() -> Message:
            nonlocal disconnected
            message = await receive()
            if message["type"] == "http.disconnect":
                disconnected = True
            return message

        async def observed_send(message: Message) -> None:
            nonlocal response_started, status, complete
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                names = {key.lower() for key, _ in headers}
                for key, value in ((b"x-content-type-options", b"nosniff"), (b"cache-control", b"no-store")):
                    if key not in names:
                        headers.append((key, value))
                message = {**message, "headers": headers}
            await send(message)
            if message["type"] == "http.response.start":
                status = message["status"]
                response_started = time.monotonic()
            elif message["type"] == "http.response.body" and not message.get("more_body", False):
                complete = True

        try:
            await self.app(scope, observed_receive, observed_send)
        except BaseException as error:
            error_type = type(error).__name__
            disconnected = disconnected or isinstance(error, OSError | asyncio.CancelledError)
            raise
        finally:
            state = scope.get("state", {})
            principal = state.get("principal")
            # Keep the existing duration-to-headers meaning. Missing response
            # headers stay null; neither a disconnect nor an exception is a
            # fabricated HTTP 200/499/500 from this observer.
            record = {
                "event": "http_request",
                "method": scope.get("method", ""),
                "path": scope.get("path", "")[:256],
                "status": status,
                "duration_ms": round(
                    ((response_started if response_started is not None else time.monotonic()) - started) * 1000, 3
                ),
                "principal_id": principal.principal_id if principal else None,
                "tenant_id": principal.tenant_id if principal else None,
                "token_id": str(principal.token_id) if principal else None,
                "request_id": str(state["fs2_request_id"]) if state.get("fs2_request_id") else None,
                "response_complete": complete,
                "disconnected": disconnected,
                "error_type": error_type,
            }
            LOGGER.info(json.dumps(record, separators=(",", ":")))
