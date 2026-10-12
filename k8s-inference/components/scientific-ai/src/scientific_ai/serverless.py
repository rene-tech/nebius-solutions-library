"""The Serverless 2 execution API (0.11.8+), without a scheduling or retry loop.

Discovery validates the actual tenant execution profile. It is not proof that
checkpoint/finalization semantics have passed live acceptance.
"""

from __future__ import annotations

import re
from typing import Protocol

import httpx

from .contracts import Attempt, Identifier, RuntimeProfile

REFERENCE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")


class UnsupportedExecution(RuntimeError):
    pass


class ServerlessError(RuntimeError):
    """Safe transport failure: upstream bodies may contain private data."""

    def __init__(self, status_code: int, *, retry_after: str | None = None):
        self.status_code = status_code
        self.retryable = status_code in (408, 429, 502, 503, 504)
        self.retry_after = retry_after if retry_after and retry_after.isdecimal() else None
        super().__init__(f"Serverless request failed (HTTP {status_code})")


def _reference(value: str) -> str:
    if not isinstance(value, str) or not REFERENCE.fullmatch(value):
        raise ValueError("Serverless references must be lowercase DNS labels (at most 63 characters)")
    return value


def _idempotency(value: str) -> str:
    if not isinstance(value, str) or not 8 <= len(value) <= 200 or not value.isascii():
        raise ValueError("idempotency_key must contain 8 to 200 ASCII characters")
    if any(ord(char) < 33 or ord(char) > 126 for char in value):
        raise ValueError("idempotency_key must contain printable non-whitespace characters")
    return value


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
    identity_ref: str | None,
    private_input_ref: str,
    checkpoint_ref: str | None = None,
) -> dict:
    """Only opaque authorized references enter ordinary job metadata.

    Resolution/authorization of refs belongs to the caller's authenticated
    application service and to Serverless. This function does not grant access.
    """
    from pydantic import TypeAdapter

    identifier = TypeAdapter(Identifier)
    for ref in (profile_ref, private_input_ref):
        _reference(ref)
    if identity_ref is not None:
        _reference(identity_ref)
    if checkpoint_ref is not None:
        identifier.validate_python(checkpoint_ref)
    payload = {
        "mode": "run",
        "input": {"protocol_ref": profile.revision},
        "execution": {
            "io_mode": "application",
            "profile_ref": profile_ref,
            "private_input_ref": private_input_ref,
            "placement": {
                "gpu_preference": list(profile.gpu_preference),
                "allowed_clusters": list(profile.allowed_clusters),
                "fallback_wait_s": profile.fallback_wait_s,
            },
            "recovery": profile.recovery.wire(checkpoint_ref),
        },
    }
    if identity_ref is not None:
        payload["execution"]["workload_identity_ref"] = identity_ref
    return payload


class ServerlessClient:
    """Configured application-to-S2 client; no public gateway/routing change.

    Supply a TLS-verifying httpx.Client with per-service authentication. The
    capability URL is operator configuration, not a customer-supplied URL.
    Do not log HTTP bodies or expose provider responses as public errors.
    """

    def __init__(self, http: httpx.Client, *, capability_path: str = "/v1/fleet"):
        if http.base_url.scheme != "https" or http.base_url.userinfo:
            raise ValueError("Serverless must use an authenticated, TLS-verifying HTTPS client")
        if re.fullmatch(r"/v1/[a-zA-Z0-9_/-]+", capability_path) is None:
            raise ValueError("capability discovery must be an operator-configured /v1/ path")
        self.http, self.capability_path = http, capability_path

    def require_capabilities(
        self,
        *,
        profile_ref: str | None = None,
        identity_ref: str | None = None,
        recovery: dict | None = None,
    ) -> dict:
        response = self.http.get(self.capability_path, follow_redirects=False)
        if response.status_code != 200:
            raise UnsupportedExecution("Serverless execution capability discovery is unavailable")
        try:
            document = response.json()
            supported = document.get("execution", {})
            if not isinstance(supported, dict) or not all(
                isinstance(supported.get(key), list) and all(isinstance(item, str) for item in supported[key])
                for key in ("io_modes", "execution_profiles", "identities")
            ):
                raise ValueError
        except (ValueError, AttributeError):
            raise UnsupportedExecution("Serverless capability response is invalid") from None
        if (
            "application" not in supported["io_modes"]
            or supported.get("private_input_dir") != "/var/run/serverless2/private"
        ):
            raise UnsupportedExecution("Serverless application I/O or private bindings are unavailable")
        if profile_ref is not None and profile_ref not in supported["execution_profiles"]:
            raise UnsupportedExecution("the requested confidential execution profile is unavailable")
        if identity_ref is not None and (
            identity_ref not in supported["identities"]
            or supported.get("identity_token_file") != "/var/run/secrets/workload-identity/token"
        ):
            raise UnsupportedExecution("the requested workload identity is unavailable")
        if recovery and recovery.get("on_infrastructure_loss") == "recover":
            advertised = supported.get("recovery")
            if (
                not isinstance(advertised, dict)
                or not isinstance(advertised.get("on_infrastructure_loss"), list)
                or "recover" not in advertised.get("on_infrastructure_loss", [])
                or type(advertised.get("max_recoveries")) is not int
                or advertised["max_recoveries"] < recovery["max_recoveries"]
            ):
                raise UnsupportedExecution("bounded automatic checkpoint recovery is unavailable")
        return supported

    def _request(self, method: str, path: str, **kwargs) -> dict:
        try:
            response = self.http.request(method, path, follow_redirects=False, **kwargs)
        except httpx.TransportError:
            # Submission may already be admitted. Never retry with a new key.
            raise ServerlessError(503) from None
        if response.status_code not in (200, 201, 202, 204):
            raise ServerlessError(response.status_code, retry_after=response.headers.get("Retry-After"))
        if response.status_code == 204:
            return {}
        try:
            document = response.json()
            if not isinstance(document, dict):
                raise ValueError
            return document
        except ValueError:
            raise ServerlessError(502) from None

    def put_binding(self, reference: str, data: dict[str, str]) -> dict:
        """Private files only; keep reference stable across a submission retry."""
        _reference(reference)
        if (
            not data
            or not all(
                isinstance(k, str)
                and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", k)
                and ".." not in k
                and isinstance(v, str)
                for k, v in data.items()
            )
            or sum(len(k.encode()) + len(v.encode()) for k, v in data.items()) > 65536
        ):
            raise ValueError("private bindings require safe filenames and at most 64 KiB of string data")
        return self._request("PUT", f"/v1/bindings/{reference}", json={"data": data})

    def delete_binding(self, reference: str) -> None:
        """Explicit grant withdrawal; never used as cleanup after uncertain admission."""
        self._request("DELETE", f"/v1/bindings/{_reference(reference)}")

    def submit(self, model_id: str, payload: dict, *, idempotency_key: str) -> dict:
        from pydantic import TypeAdapter

        TypeAdapter(Identifier).validate_python(model_id)
        _idempotency(idempotency_key)
        if payload.get("execution", {}).get("io_mode") != "application":
            raise ValueError("this integration requires explicit application-managed I/O")
        execution = payload["execution"]
        if not execution.get("profile_ref") or not execution.get("private_input_ref"):
            raise ValueError("application execution requires a confidential profile and private binding")
        self.require_capabilities(
            profile_ref=execution["profile_ref"],
            identity_ref=execution.get("workload_identity_ref"),
            recovery=execution.get("recovery"),
        )
        return self._request(
            "POST",
            f"/v1/models/{model_id}:invoke",
            json=payload,
            headers={"Idempotency-Key": idempotency_key},
        )

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
            _idempotency(idempotency_key)
            return self._request("POST", path + ":cancel", headers={"Idempotency-Key": idempotency_key})
        document = self._request("GET", path)
        if (
            (document.get("execution") or {}).get("io_mode") == "application"
            and document.get("status") == "SUCCEEDED"
            and not document.get("result_ref")
        ):
            # S2 0.11.8 can report a Complete Job as successful when its durable
            # attempt record is absent. Fail closed at our boundary as well.
            raise ServerlessError(503)
        return document

    def result(self, operation_id: str) -> dict:
        from pydantic import TypeAdapter

        TypeAdapter(Identifier).validate_python(operation_id)
        document = self._request("GET", f"/v1/operations/{operation_id}/result")
        if document.get("status") == "SUCCEEDED" and not document.get("result_ref"):
            raise ServerlessError(503)
        return document

    def resume(
        self,
        operation_id: str,
        *,
        idempotency_key: str,
        checkpoint_ref: str | None = None,
        gpu_class: str | None = None,
    ) -> dict:
        """S2 0.11.11 binds one continuation to the key and request payload.

        With no explicit reference, S2 selects the newest acknowledged checkpoint,
        including after loss of the old execution volume.
        """
        from pydantic import TypeAdapter

        from .contracts import Digest

        TypeAdapter(Identifier).validate_python(operation_id)
        _idempotency(idempotency_key)
        body = {}
        if checkpoint_ref is not None:
            TypeAdapter(Digest).validate_python(checkpoint_ref)
            body["checkpoint_ref"] = checkpoint_ref
        if gpu_class is not None:
            body["gpu_class"] = _reference(gpu_class)
        return self._request(
            "POST",
            f"/v1/operations/{operation_id}:resume",
            json=body,
            headers={"Idempotency-Key": idempotency_key},
        )
