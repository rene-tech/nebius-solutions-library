"""Public health and static-bearer discovery response boundaries."""

from __future__ import annotations

import json

from starlette.types import ASGIApp, Message, Receive, Scope, Send

PUBLIC_READINESS_PATH = "/readyz"
PROTECTED_RESOURCE_PATHS = frozenset(
    {
        "/.well-known/oauth-protected-resource",
        "/.well-known/oauth-protected-resource/mcp",
    }
)
_REPLACED_HEADERS = frozenset({b"cache-control", b"content-length", b"content-type"})


class PublicSurfaceBoundary:
    """Minimize unauthenticated health and static-bearer metadata responses.

    The wrapped application still performs its complete readiness evaluation.
    Only the public representation is replaced, so status-based probes retain
    their existing semantics without exposing dependency or model detail.
    Likewise, both MCP protected-resource aliases retain the SDK and edge
    checks beneath this boundary while no longer claiming an OAuth issuer.
    """

    def __init__(self, app: ASGIApp, *, mcp_resource: str) -> None:
        self.app = app
        self.mcp_resource = mcp_resource

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("method") != "GET":
            await self.app(scope, receive, send)
            return

        path = str(scope.get("path", ""))
        if path != PUBLIC_READINESS_PATH and path not in PROTECTED_RESOURCE_PATHS:
            await self.app(scope, receive, send)
            return

        messages: list[Message] = []

        async def capture(message: Message) -> None:
            messages.append(message)

        await self.app(scope, receive, capture)
        response_start = next((message for message in messages if message["type"] == "http.response.start"), None)
        if response_start is None:
            for message in messages:
                await send(message)
            return

        status_code = int(response_start["status"])
        payload: dict[str, object] | None = None
        if path == PUBLIC_READINESS_PATH and status_code in {200, 503}:
            payload = {"status": "ready" if status_code == 200 else "unavailable"}
        elif path in PROTECTED_RESOURCE_PATHS and status_code == 200:
            payload = {
                "resource": self.mcp_resource,
                "bearer_methods_supported": ["header"],
            }

        if payload is None:
            for message in messages:
                await send(message)
            return

        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        headers = [
            (name, value)
            for name, value in response_start.get("headers", [])
            if name.lower() not in _REPLACED_HEADERS
        ]
        headers.extend(
            [
                (b"cache-control", b"no-store"),
                (b"content-length", str(len(body)).encode("ascii")),
                (b"content-type", b"application/json"),
            ]
        )
        await send({**response_start, "headers": headers})
        await send({"type": "http.response.body", "body": body})
