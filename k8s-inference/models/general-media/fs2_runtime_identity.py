"""Pod-local response attribution for ASGI runtimes; no body buffering or routing.

Load with vLLM's ``--middleware fs2_runtime_identity.RuntimeIdentityMiddleware``.
FS2_RUNTIME_POD_UID must come from the Kubernetes downward API, not a request.
The gateway still verifies the response against Kubernetes and kubelet facts.
"""

from __future__ import annotations

import os
from uuid import UUID

HEADERS = (
    b"x-fs2-runtime-pod-uid",
    b"x-fs2-runtime-operation-id",
    b"x-fs2-runtime-attempt",
)


class RuntimeIdentityMiddleware:
    def __init__(self, app):
        self.app = app
        self.pod_uid = str(UUID(os.environ["FS2_RUNTIME_POD_UID"])).encode("ascii")

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = scope.get("headers", [])
        operations = [v for k, v in headers if k.lower() == b"x-fs2-operation-id"]
        requests = [v for k, v in headers if k.lower() == b"x-request-id"]
        identity = []
        if len(operations) == len(requests) == 1:
            try:
                operation = str(UUID(operations[0].decode("ascii"))).encode("ascii")
                prefix, attempt = requests[0].rsplit(b":", 1)
                if (
                    prefix == operation
                    and attempt.isdigit()
                    and 1 <= len(attempt) <= 9
                    and 1 <= int(attempt) <= 999_999_999
                ):
                    identity = list(
                        zip(HEADERS, (self.pod_uid, operation, attempt), strict=True)
                    )
            except (ValueError, UnicodeError):
                pass

        async def attributed_send(message):
            if message["type"] == "http.response.start":
                # An application or client cannot supply an alternative identity.
                message = {
                    **message,
                    "headers": [
                        (k, v)
                        for k, v in message.get("headers", [])
                        if k.lower() not in HEADERS
                    ]
                    + identity,
                }
            await send(message)

        await self.app(scope, receive, attributed_send)
