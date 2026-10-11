"""The sole provisional wire seam to S2-01..05. No scheduling or retry loop.

The S2 team still owns the final capability schema and execution field names.
An unsupported server is an error, never a reason to use legacy tenant keys.
"""

from __future__ import annotations

import re
from typing import Protocol

import httpx

from .contracts import Attempt, Identifier, RuntimeProfile

REQUIRED_CAPABILITIES = frozenset(
    {
        "application_io",
        "scoped_workload_identity",
        "private_input",
        "attempt_fencing",
        "durable_finalization",
        "portable_checkpoints",
        "placement_time_images",
    }
)


class UnsupportedExecution(RuntimeError):
    pass


class Lifecycle(Protocol):
    """Authenticated S2 adapter. Methods must reject stale/cancelled attempts.

    Publish/finalize are durable and idempotent for the same attempt/ref. Their
    implementation must compare the epoch atomically, not check then write.
    This contract deliberately has no local/always-successful production stub.
    """

    def require_current(self, attempt: Attempt) -> None: ...

    def checkpoint(self, attempt: Attempt, reference: str, generation: int) -> None: ...

    def finalize(self, attempt: Attempt, reference: str) -> None: ...


def invoke_payload(
    profile: RuntimeProfile,
    *,
    profile_ref: str,
    identity_ref: str,
    private_input_ref: str,
    checkpoint_ref: str | None = None,
) -> dict:
    """Only opaque authorized references enter ordinary job metadata.

    Resolution/authorization of refs belongs to the caller's authenticated
    application service and to Serverless. This function does not grant access.
    """
    from pydantic import TypeAdapter

    identifier = TypeAdapter(Identifier)
    refs = [profile_ref, identity_ref, private_input_ref]
    if checkpoint_ref is not None:
        refs.append(checkpoint_ref)
    for ref in refs:
        identifier.validate_python(ref)
    return {
        "mode": "run",
        "input": {"protocol_ref": profile.revision},
        "execution": {
            "io_mode": "application",
            "profile_ref": profile_ref,
            "workload_identity_ref": identity_ref,
            "private_input_ref": private_input_ref,
            "placement": {
                "gpu_preference": list(profile.gpu_preference),
                "allowed_clusters": list(profile.allowed_clusters),
                "fallback_wait_s": profile.fallback_wait_s,
            },
            "recovery": {"on_infrastructure_loss": "retry", "checkpoint_ref": checkpoint_ref},
        },
    }


class ServerlessClient:
    """Configured application-to-S2 client; no public gateway/routing change.

    Supply a TLS-verifying httpx.Client with per-service authentication. The
    capability URL is operator configuration, not a customer-supplied URL.
    Do not log HTTP bodies or expose provider responses as public errors.
    """

    def __init__(self, http: httpx.Client, *, capability_path: str):
        if http.base_url.scheme != "https" or http.base_url.userinfo:
            raise ValueError("Serverless must use an authenticated, TLS-verifying HTTPS client")
        if re.fullmatch(r"/v1/[a-zA-Z0-9_/-]+", capability_path) is None:
            raise ValueError("capability discovery must be an operator-configured /v1/ path")
        self.http, self.capability_path = http, capability_path

    def require_capabilities(self) -> None:
        response = self.http.get(self.capability_path, follow_redirects=False)
        if response.status_code != 200:
            raise UnsupportedExecution("Serverless execution capability discovery is unavailable")
        try:
            document = response.json()
            supported = document.get("execution", {})
            if not isinstance(supported, dict):
                raise ValueError
        except (ValueError, AttributeError):
            raise UnsupportedExecution("Serverless capability response is invalid") from None
        missing = [key for key in sorted(REQUIRED_CAPABILITIES) if supported.get(key) is not True]
        if missing:
            raise UnsupportedExecution("Serverless execution capabilities missing: " + ", ".join(missing))

    def submit(self, model_id: str, payload: dict, *, idempotency_key: str) -> dict:
        from pydantic import TypeAdapter

        for item in (model_id, idempotency_key):
            TypeAdapter(Identifier).validate_python(item)
        if payload.get("execution", {}).get("io_mode") != "application":
            raise ValueError("this integration requires explicit application-managed I/O")
        self.require_capabilities()  # never submit unknown security fields to an old server
        response = self.http.post(
            f"/v1/models/{model_id}:invoke",
            json=payload,
            headers={"Idempotency-Key": idempotency_key},
            follow_redirects=False,
        )
        if response.status_code not in (200, 201, 202):
            raise RuntimeError(f"Serverless submission failed (HTTP {response.status_code})")
        return response.json()

    def status(self, operation_id: str) -> dict:
        """Pass through authoritative S2 state; never infer success from pod exit."""
        return self._operation(operation_id, cancel=False)

    def cancel(self, operation_id: str, *, idempotency_key: str) -> dict:
        return self._operation(operation_id, cancel=True, idempotency_key=idempotency_key)

    def _operation(self, operation_id: str, *, cancel: bool, idempotency_key: str | None = None) -> dict:
        from pydantic import TypeAdapter

        TypeAdapter(Identifier).validate_python(operation_id)
        path = f"/v1/operations/{operation_id}"
        if cancel:
            TypeAdapter(Identifier).validate_python(idempotency_key)
            response = self.http.post(
                path + ":cancel", headers={"Idempotency-Key": idempotency_key}, follow_redirects=False
            )
        else:
            response = self.http.get(path, follow_redirects=False)
        if response.status_code != 200:
            raise RuntimeError(f"Serverless operation request failed (HTTP {response.status_code})")
        return response.json()
