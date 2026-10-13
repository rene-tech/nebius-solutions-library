"""Native endpoint transport and owner-scoped result receipts, never a scheduler.

The original request's ServerlessClient supplies authentication and admission.
A receipt records a returned S2 operation ID; it does not invent asynchronous
execution, idempotency or a replacement operation identity.
"""

from __future__ import annotations

import httpx
from fastapi import HTTPException

from .endpoint_catalog import catalog, contract_for
from .endpoint_contracts import NativeResultError, dispatch_native
from .gateway_store import identifier
from .serverless import ServerlessError

# The source contracts have one native operation each. These names are the
# legacy catalog policy operations, not inferred from an LLM or tool name.
OPERATIONS = {
    "admet-ai": "screen-molecules",
    "ctoxpred2": "screen-molecules",
    "altumage": "predict-age",
    "phenoage": "predict-age",
    "cellpose-cpsam-v2": "segment-cells",
    "sam2-1-hiera-large": "segment-track-media",
    "molmim": "generate-molecule",
    "boltz2": "predict",
    "openfold2": "predict",
    "openfold3": "predict",
    "msa-search-pdb70": "search-msa",
    "proteinmpnn": "design-protein",
    "diffdock": "dock",
}


def has_scope(info: dict, scope: str) -> bool:
    """Match S2's unscoped-key semantics without widening imported app scopes."""
    if scope == "mcp.invoke":
        legacy = info.get("legacy") or {}
        # Import retains original app scopes separately from generic S2 scopes.
        original = legacy.get("original_scopes", (legacy.get("metadata") or {}).get("original_scopes"))
        return original is None or scope in original
    scopes = info.get("scopes") or []
    return not scopes or scope in scopes


def model_allowed(info: dict, model_id: str) -> bool:
    allowed = info.get("models") or []
    return not allowed or model_id in allowed or "all-proxy-models" in allowed


def authorize(info: dict, *, model_id: str | None = None, scope: str | None = None):
    if model_id is not None and not model_allowed(info, model_id):
        raise HTTPException(403, "this key is not authorized for the selected App")
    if scope is not None and not has_scope(info, scope):
        raise HTTPException(403, "this key lacks the required request scope")


class NativeCallError(RuntimeError):
    def __init__(self, status_code: int, *, operation_id: str | None = None, unknown: bool = False):
        self.status_code, self.operation_id, self.unknown = status_code, operation_id, unknown
        super().__init__("Native endpoint request failed")

    def public(self):
        return {
            "code": "NATIVE_ENDPOINT_FAILED",
            "status_code": self.status_code,
            "operation_id": self.operation_id,
            "admission": "accepted" if self.operation_id else "unknown" if self.unknown else "not-confirmed",
            # Sync transport failure is never permission to repeat expensive work.
            "retryable": False,
        }


class NativeCapabilityError(ValueError):
    def public(self):
        return {
            "code": "NATIVE_EXECUTION_OPTION_UNSUPPORTED",
            "retryable": False,
            "admission": "not-submitted",
            "message": (
                "This endpoint supports synchronous calls only; durable async, "
                "wait handles and idempotency are not implemented."
            ),
        }


def _operation(response: dict) -> dict:
    value = response.get("operation")
    if isinstance(value, str):
        value = {"id": value}
    if (
        not isinstance(value, dict)
        or not isinstance(value.get("id"), str)
        or not 1 <= len(value["id"]) <= 256
    ):
        raise NativeResultError("S2 did not supply a valid operation identity")
    return {key: value[key] for key in ("id", "status") if key in value}


class EndpointGateway:
    def __init__(self, *, timeout_seconds: int = 900):
        if not 1 <= timeout_seconds <= 900:
            raise ValueError("native timeout exceeds the configured qualified maximum")
        self.timeout_seconds = timeout_seconds

    @staticmethod
    def synchronous_options(*, headers=None, options=None):
        headers, options = headers or {}, options or {}
        if any(
            name in headers for name in ("idempotency-key", "x-fs2-wait-seconds", "x-fs2-deadline-seconds")
        ):
            raise NativeCapabilityError()
        if any(name in options for name in ("idempotency_key", "wait_seconds", "async")):
            raise NativeCapabilityError()

    @staticmethod
    def _binding(model: dict) -> bool:
        return model.get("protocol") == "native" and "sync" in (model.get("modes") or [])

    def available(self, client, info: dict) -> dict[str, dict]:
        authorize(info, scope="read:catalog")
        try:
            response = client.http.get("/v1/models", follow_redirects=False)
        except httpx.TransportError:
            raise ServerlessError(503) from None
        # Older GROMACS-only pilot APIs do not implement model discovery. Never
        # manufacture native entries when this route is unavailable.
        if response.status_code == 404:
            return {}
        if response.status_code != 200:
            raise ServerlessError(response.status_code)
        try:
            models = response.json()
        except ValueError:
            raise ServerlessError(502) from None
        if not isinstance(models, list) or not all(isinstance(row, dict) for row in models):
            raise ServerlessError(502)
        return {
            row["id"]: row
            for row in models
            if row.get("id") in catalog() and self._binding(row) and model_allowed(info, row["id"])
        }

    def describe(self, model_id: str, live: dict) -> dict:
        contract = contract_for(model_id)
        return {
            **contract.describe(),
            "operations": [OPERATIONS[model_id]],
            "runtime_identity": live.get("image"),
            "readiness": "observed-in-serverless",
            "regions": live.get("regions", []),
            "execution": {**contract.describe()["execution"], "timeout_seconds": self.timeout_seconds},
        }

    @staticmethod
    def _key(store, operation_id: str):
        return identifier(store.owner, "native-operation", operation_id)

    def receipt(self, store, info: dict, operation_id: str, *, result=False):
        authorize(info, scope="read:results" if result else "read:operations")
        value = store.get("native-operations", self._key(store, operation_id))
        if value.get("operation", {}).get("id") != operation_id:
            raise KeyError(operation_id)
        authorize(info, model_id=value["model_id"])
        if result:
            if value.get("publication") != "complete":
                raise NativeResultError(
                    "The native result is not durably published", operation_id=operation_id
                )
            return {k: value[k] for k in ("model_id", "operation", "result")}
        return {k: value[k] for k in ("model_id", "operation", "publication")}

    def invoke(self, client, store, info: dict, model_id: str, payload: dict, *, surface="http"):
        authorize(info, model_id=model_id, scope="invoke:sync")
        contract = contract_for(model_id)
        self.synchronous_options(options=payload)
        live = client._request("GET", f"/v1/models/{model_id}")
        if live.get("id") != model_id or not self._binding(live):
            raise HTTPException(409, "this App has no synchronous native binding")

        def invoke_native(exact_model: str, native: dict):
            try:
                response = client.http.post(
                    f"/v1/models/{exact_model}:invoke",
                    json={"mode": "sync", "input": native, "timeout_s": self.timeout_seconds},
                    timeout=self.timeout_seconds + 15,
                    follow_redirects=False,
                )
            except httpx.TransportError:
                raise NativeCallError(504, unknown=True) from None
            try:
                value = response.json()
            except ValueError:
                raise NativeCallError(502, unknown=True) from None
            if not isinstance(value, dict):
                raise NativeCallError(502, unknown=True)
            if "operation" not in value and response.status_code != 200:
                raise NativeCallError(response.status_code, unknown=response.status_code >= 500)
            operation = _operation(value)
            receipt = {"model_id": model_id, "operation": operation, "publication": "pending"}
            try:
                store.save("native-operations", self._key(store, operation["id"]), receipt, create=True)
            except Exception:
                raise NativeResultError(
                    "S2 returned but its result receipt could not be published", operation_id=operation["id"]
                ) from None
            if response.status_code != 200 or operation.get("status") == "FAILED":
                raise NativeCallError(
                    response.status_code if 400 <= response.status_code < 600 else 502,
                    operation_id=operation["id"],
                )
            return value

        value = dispatch_native(contract, payload, store=store, invoke=invoke_native, surface=surface)
        operation_id = value["operation"]["id"]
        try:
            store.save(
                "native-operations", self._key(store, operation_id), {**value, "publication": "complete"}
            )
        except Exception:
            raise NativeResultError(
                "Native results returned but their receipt could not be published", operation_id=operation_id
            ) from None
        return value
