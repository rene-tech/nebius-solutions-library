"""Secret-safe public HTTP and MCP qualification for every retained live model."""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import ipaddress
import json
import logging
import os
import re
import stat
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
import httpx2
from fs2_serve_catalog.loader import Catalog, CatalogError, load_catalog
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

from .live_release import LiveRelease, LiveReleaseError, canonical_json, render_live_release

ACCEPTANCE_SCHEMA = "fs2-serve.nebius.ai/all-models-live-acceptance/v1"
EVIDENCE_SCHEMA = "fs2-serve.nebius.ai/all-models-live-acceptance-evidence/v2"
MCP_PROTOCOL_VERSION = "2026-07-28"
TLS_MODE_VERIFIED = "verified"
TLS_MODE_DISPOSABLE_STAGING = "disposable-staging-insecure"
TLS_MODES = (TLS_MODE_VERIFIED, TLS_MODE_DISPOSABLE_STAGING)
_TERMINAL = frozenset({"succeeded", "failed", "cancelled", "preempted", "expired"})
_SAFE_CODE = re.compile(r"[a-z][a-z0-9_]{0,95}")
_SHA256 = re.compile(r"[a-f0-9]{64}")
_MAX_TOKEN_BYTES = 4096
_MAX_RESPONSE_BYTES = 64 * 1024 * 1024
_MAX_CASE_BYTES = 16 * 1024 * 1024


class AcceptanceError(RuntimeError):
    """A value-suppressed failure that is safe to persist."""

    def __init__(self, code: str) -> None:
        if _SAFE_CODE.fullmatch(code) is None:
            raise ValueError("acceptance failure code is unsafe")
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class AcceptanceCase:
    model_id: str
    revision: str
    protocol: str
    operation: str
    payload: dict[str, Any]
    payload_sha256: str
    response_kind: str


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def read_token_file(path: Path) -> str:
    """Read one PAT from an absolute owner-held mode-0600 file without following links."""

    if not path.is_absolute():
        raise AcceptanceError("token_path_not_absolute")
    flags = os.O_RDONLY | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError:
        raise AcceptanceError("token_file_unavailable") from None
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise AcceptanceError("token_file_not_regular")
        if stat.S_IMODE(metadata.st_mode) != 0o600:
            raise AcceptanceError("token_file_mode_invalid")
        if metadata.st_uid != os.geteuid():
            raise AcceptanceError("token_file_owner_invalid")
        if not 1 <= metadata.st_size <= _MAX_TOKEN_BYTES:
            raise AcceptanceError("token_file_size_invalid")
        value = os.read(descriptor, _MAX_TOKEN_BYTES + 1)
    finally:
        os.close(descriptor)
    if value.endswith(b"\n"):
        value = value[:-1]
    if not 32 <= len(value) <= _MAX_TOKEN_BYTES or any(character < 33 or character > 126 for character in value):
        raise AcceptanceError("token_file_content_invalid")
    try:
        return value.decode("ascii")
    except UnicodeDecodeError:
        raise AcceptanceError("token_file_content_invalid") from None


def validate_origin(value: str) -> str:
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
        or parsed.port not in {None, 443}
    ):
        raise AcceptanceError("endpoint_identity_invalid")
    return f"https://{parsed.hostname}"


def tls_verify_for_mode(origin: str, mode: str) -> bool:
    """Resolve the explicit TLS policy without weakening the default path."""

    normalized = validate_origin(origin)
    if mode == TLS_MODE_VERIFIED:
        return True
    if mode != TLS_MODE_DISPOSABLE_STAGING:
        raise AcceptanceError("tls_mode_invalid")
    hostname = urlsplit(normalized).hostname
    try:
        address = ipaddress.ip_address(hostname or "")
    except ValueError:
        raise AcceptanceError("disposable_staging_tls_endpoint_invalid") from None
    if not isinstance(address, ipaddress.IPv4Address) or not address.is_global:
        raise AcceptanceError("disposable_staging_tls_endpoint_invalid")
    return False


def _exact(value: object, fields: set[str], code: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != fields:
        raise AcceptanceError(code)
    return value


def semantic_payload_sha256(payload: object, serialization: str) -> str:
    canonical = canonical_json(payload)
    if serialization == "sha256-canonical-json-newline/v1":
        canonical += b"\n"
    elif serialization != "sha256-canonical-json-no-newline/v1":
        raise AcceptanceError("acceptance_case_serialization_invalid")
    return sha256_bytes(canonical)


def _replace_exact_asset_uri(value: Any, uri: str, data_uri: str) -> tuple[Any, int]:
    """Return a JSON-shaped copy with one exact licensed URI materialized."""

    if isinstance(value, dict):
        replaced: dict[str, Any] = {}
        count = 0
        for key, item in value.items():
            replacement, item_count = _replace_exact_asset_uri(item, uri, data_uri)
            replaced[key] = replacement
            count += item_count
        return replaced, count
    if isinstance(value, list):
        replaced_items: list[Any] = []
        count = 0
        for item in value:
            replacement, item_count = _replace_exact_asset_uri(item, uri, data_uri)
            replaced_items.append(replacement)
            count += item_count
        return replaced_items, count
    return (data_uri, 1) if value == uri else (value, 0)


def _read_packaged_jpeg(path: Path, *, expected_sha256: str, expected_bytes: int) -> bytes:
    """Read one immutable public test fixture without following a symlink."""

    flags = os.O_RDONLY | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError:
        raise AcceptanceError("acceptance_asset_file_unavailable") from None
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size != expected_bytes:
            raise AcceptanceError("acceptance_asset_file_invalid")
        chunks: list[bytes] = []
        remaining = expected_bytes + 1
        while remaining:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
    finally:
        os.close(descriptor)
    if len(raw) != expected_bytes or sha256_bytes(raw) != expected_sha256 or not raw.startswith(b"\xff\xd8\xff"):
        raise AcceptanceError("acceptance_asset_file_invalid")
    return raw


def _materialize_payload_assets(
    payload: dict[str, Any],
    payload_sha256: str,
    semantic_document: dict[str, Any],
    cases_path: Path,
) -> dict[str, Any]:
    """Replace canonical remote image subjects with verified local data URIs.

    The catalog digest remains bound to the readable licensed HTTPS subject.
    The live wire request is self-contained, so acceptance never depends on a
    third-party host being reachable or willing to serve the cluster egress IP.
    """

    requests = semantic_document.get("requests")
    assets = semantic_document.get("assets")
    if not isinstance(requests, list) or not isinstance(assets, list):
        raise AcceptanceError("acceptance_asset_contract_invalid")
    request_ids = {
        item.get("id") for item in requests if isinstance(item, dict) and item.get("payload_sha256") == payload_sha256
    }
    if len(request_ids) != 1:
        raise AcceptanceError("acceptance_asset_contract_invalid")
    request_id = next(iter(request_ids))
    selected = [item for item in assets if isinstance(item, dict) and item.get("request_id") == request_id]
    materialized: Any = payload
    for asset in selected:
        uri = asset.get("uri")
        digest = asset.get("content_sha256")
        byte_count = asset.get("bytes")
        if (
            asset.get("kind") != "licensed-image"
            or not isinstance(uri, str)
            or not isinstance(digest, str)
            or _SHA256.fullmatch(digest) is None
            or not isinstance(byte_count, int)
            or not 1 <= byte_count <= _MAX_CASE_BYTES
        ):
            raise AcceptanceError("acceptance_asset_contract_invalid")
        raw = _read_packaged_jpeg(
            cases_path.parent / "assets" / f"{digest}.jpg",
            expected_sha256=digest,
            expected_bytes=byte_count,
        )
        data_uri = "data:image/jpeg;base64," + base64.b64encode(raw).decode("ascii")
        materialized, count = _replace_exact_asset_uri(materialized, uri, data_uri)
        if count != 1:
            raise AcceptanceError("acceptance_asset_reference_invalid")
    if not isinstance(materialized, dict) or len(canonical_json(materialized)) > _MAX_CASE_BYTES:
        raise AcceptanceError("acceptance_case_payload_invalid")
    return materialized


def _load_cases(path: Path, catalog: Catalog, release: LiveRelease) -> tuple[AcceptanceCase, ...]:
    try:
        raw = path.read_bytes()
    except OSError:
        raise AcceptanceError("acceptance_cases_unavailable") from None
    if not raw or len(raw) > _MAX_CASE_BYTES:
        raise AcceptanceError("acceptance_cases_size_invalid")

    def reject_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise AcceptanceError("acceptance_cases_duplicate_key")
            result[key] = value
        return result

    try:
        document = json.loads(raw, object_pairs_hook=reject_duplicates)
    except (json.JSONDecodeError, UnicodeDecodeError, RecursionError):
        raise AcceptanceError("acceptance_cases_json_invalid") from None
    document = _exact(document, {"schema", "cases"}, "acceptance_cases_shape_invalid")
    if document["schema"] != ACCEPTANCE_SCHEMA or not isinstance(document["cases"], dict):
        raise AcceptanceError("acceptance_cases_schema_invalid")
    routes = {route["model_id"]: route for route in release.routes}
    if set(document["cases"]) != set(routes):
        raise AcceptanceError("acceptance_cases_model_set_invalid")

    cases: list[AcceptanceCase] = []
    for model_id in sorted(routes):
        item = _exact(
            document["cases"][model_id],
            {"protocol", "operation", "payload", "payload_sha256", "response_kind"},
            "acceptance_case_shape_invalid",
        )
        route = routes[model_id]
        protocol = item["protocol"]
        operation = item["operation"]
        payload = item["payload"]
        payload_digest = item["payload_sha256"]
        response_kind = item["response_kind"]
        if not isinstance(protocol, str) or protocol not in route["protocols"]:
            raise AcceptanceError("acceptance_case_protocol_invalid")
        if not isinstance(operation, str) or operation not in route["operations"]:
            raise AcceptanceError("acceptance_case_operation_invalid")
        if not isinstance(payload, dict) or not payload:
            raise AcceptanceError("acceptance_case_payload_invalid")
        if not isinstance(payload_digest, str) or _SHA256.fullmatch(payload_digest) is None:
            raise AcceptanceError("acceptance_case_digest_invalid")
        semantic = catalog.semantic_request_contract(model_id)
        semantic_document = semantic.to_dict()
        serialization = semantic_document.get("serialization")
        if not isinstance(serialization, str) or semantic_payload_sha256(payload, serialization) != payload_digest:
            raise AcceptanceError("acceptance_case_payload_drift")
        if semantic.state != "qualified" or payload_digest not in semantic.request_sha256:
            raise AcceptanceError("acceptance_case_not_canonical")
        invocation = semantic.invocation
        if invocation.get("protocol") != protocol or invocation.get("operation") != operation:
            raise AcceptanceError("acceptance_case_invocation_drift")
        if protocol.startswith("openai-") and (payload.get("model") != model_id or payload.get("stream") is True):
            raise AcceptanceError("acceptance_case_openai_payload_invalid")
        if response_kind not in {"json-object", "openai-chat", "png-b64-json", "nifti-b64-json"}:
            raise AcceptanceError("acceptance_case_response_kind_invalid")
        payload = _materialize_payload_assets(
            payload,
            payload_digest,
            semantic_document,
            path,
        )
        cases.append(
            AcceptanceCase(
                model_id=model_id,
                revision=route["model_revision"],
                protocol=protocol,
                operation=operation,
                payload=payload,
                payload_sha256=payload_digest,
                response_kind=response_kind,
            )
        )
    return tuple(cases)


def response_summary(value: object, raw: bytes, kind: str) -> dict[str, object]:
    if not isinstance(value, dict) or not value:
        raise AcceptanceError("semantic_response_schema_invalid")
    summary: dict[str, object] = {"response_sha256": sha256_bytes(raw), "response_bytes": len(raw)}
    if kind == "openai-chat":
        choices = value.get("choices")
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            raise AcceptanceError("semantic_response_schema_invalid")
        message = choices[0].get("message")
        if (
            not isinstance(message, dict)
            or not isinstance(message.get("content"), str)
            or not message["content"].strip()
        ):
            raise AcceptanceError("semantic_response_schema_invalid")
        summary["content_sha256"] = sha256_bytes(message["content"].encode("utf-8"))
    elif kind == "png-b64-json":
        data = value.get("data")
        if not isinstance(data, list) or not data or not isinstance(data[0], dict):
            raise AcceptanceError("semantic_response_schema_invalid")
        encoded = data[0].get("b64_json")
        if not isinstance(encoded, str):
            raise AcceptanceError("semantic_response_schema_invalid")
        try:
            decoded = base64.b64decode(encoded, validate=True)
        except ValueError:
            raise AcceptanceError("semantic_response_schema_invalid") from None
        if not decoded.startswith(b"\x89PNG\r\n\x1a\n"):
            raise AcceptanceError("semantic_response_schema_invalid")
        summary.update({"artifact_sha256": sha256_bytes(decoded), "artifact_bytes": len(decoded)})
    elif kind == "nifti-b64-json":
        encoded = value.get("output_nifti_base64")
        if not isinstance(encoded, str):
            raise AcceptanceError("semantic_response_schema_invalid")
        try:
            decoded = base64.b64decode(encoded, validate=True)
        except ValueError:
            raise AcceptanceError("semantic_response_schema_invalid") from None
        if not decoded.startswith(b"\x1f\x8b"):
            raise AcceptanceError("semantic_response_schema_invalid")
        summary.update({"artifact_sha256": sha256_bytes(decoded), "artifact_bytes": len(decoded)})
    return summary


def _safe_failure(error: BaseException) -> str:
    if isinstance(error, AcceptanceError):
        return error.code
    if isinstance(error, TimeoutError | asyncio.TimeoutError | httpx.TimeoutException):
        return "bounded_timeout"
    if isinstance(error, OSError | httpx.HTTPError):
        return "transport_failure"
    return "unexpected_failure"


def _mcp_result(value: Any) -> dict[str, Any]:
    if getattr(value, "is_error", True) is not False:
        raise AcceptanceError("mcp_tool_error")
    structured = getattr(value, "structured_content", None)
    if not isinstance(structured, dict):
        raise AcceptanceError("mcp_tool_result_invalid")
    return structured


def discover_model_tools(tools: list[Any], cases: tuple[AcceptanceCase, ...]) -> dict[str, str]:
    expected = {(case.model_id, case.revision, case.protocol): case.model_id for case in cases}
    discovered: dict[str, str] = {}
    for tool in tools:
        try:
            value = tool.model_dump(mode="json", by_alias=True)
        except (AttributeError, TypeError, ValueError):
            continue
        if not isinstance(value, dict):
            continue
        metadata = value.get("_meta", value.get("meta"))
        if not isinstance(metadata, dict):
            continue
        identity = metadata.get("fs2_model_id")
        revision = metadata.get("fs2_model_revision")
        protocol = metadata.get("fs2_protocol")
        if not isinstance(identity, str) or not isinstance(revision, str) or not isinstance(protocol, str):
            continue
        key = (identity, revision, protocol)
        model_id = expected.get(key)
        name = value.get("name")
        if model_id is None or not isinstance(name, str) or not name or model_id in discovered:
            continue
        discovered[model_id] = name
    if set(discovered) != {case.model_id for case in cases}:
        raise AcceptanceError("mcp_model_tool_set_invalid")
    return discovered


class AcceptanceRunner:
    def __init__(
        self,
        *,
        origin: str,
        token: str,
        release: LiveRelease,
        cases: tuple[AcceptanceCase, ...],
        timeout_seconds: float,
        concurrency: int,
        tls_mode: str = TLS_MODE_VERIFIED,
    ) -> None:
        self.origin = validate_origin(origin)
        self.tls_mode = tls_mode
        tls_verify = tls_verify_for_mode(self.origin, tls_mode)
        self.token = token
        self.release = release
        self.cases = cases
        self.deadline = time.monotonic() + timeout_seconds
        self.concurrency = concurrency
        self.nonce = uuid4().hex
        common: dict[str, Any] = {
            "base_url": self.origin,
            "timeout": httpx.Timeout(connect=15.0, read=40.0, write=20.0, pool=10.0),
            "follow_redirects": False,
            "trust_env": False,
            "verify": tls_verify,
        }
        self.public = httpx.AsyncClient(**common)
        self.authorized = httpx.AsyncClient(
            **common,
            headers={"authorization": f"Bearer {token}", "origin": self.origin},
        )
        self.mcp_http = httpx2.AsyncClient(
            headers={"authorization": f"Bearer {token}", "origin": self.origin},
            follow_redirects=False,
            trust_env=False,
            verify=tls_verify,
        )

    async def close(self) -> None:
        await asyncio.gather(self.public.aclose(), self.authorized.aclose(), self.mcp_http.aclose())

    def remaining(self) -> float:
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise AcceptanceError("acceptance_deadline_elapsed")
        return remaining

    async def _json(
        self,
        client: httpx.AsyncClient,
        method: str,
        path: str,
        *,
        payload: object | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, dict[str, str], object, bytes]:
        async with client.stream(method, path, json=payload, headers=headers) as response:
            raw = bytearray()
            async for chunk in response.aiter_bytes():
                raw.extend(chunk)
                if len(raw) > _MAX_RESPONSE_BYTES:
                    raise AcceptanceError("response_too_large")
            selected = {
                key: value
                for key in ("x-fs2-operation-id", "location", "retry-after")
                if (value := response.headers.get(key)) is not None
            }
            try:
                decoded: object = json.loads(raw) if raw else None
            except (json.JSONDecodeError, UnicodeDecodeError, RecursionError):
                raise AcceptanceError("response_json_invalid") from None
            return response.status_code, selected, decoded, bytes(raw)

    async def preflight(self) -> dict[str, object]:
        status, _, _, _ = await self._json(self.public, "GET", "/v1/models")
        if status != 401:
            raise AcceptanceError("public_route_not_protected")
        status, _, value, _ = await self._json(self.authorized, "GET", "/v1/models")
        if status != 200 or not isinstance(value, dict) or not isinstance(value.get("data"), list):
            raise AcceptanceError("authorized_model_listing_invalid")
        expected = {case.model_id: case for case in self.cases}
        listed = value["data"]
        if len(listed) != len(expected) or any(not isinstance(item, dict) for item in listed):
            raise AcceptanceError("authorized_model_set_invalid")
        identities = {item.get("id"): item for item in listed}
        if set(identities) != set(expected):
            raise AcceptanceError("authorized_model_set_invalid")
        for model_id, case in expected.items():
            item = identities[model_id]
            if item.get("revision") != case.revision or item.get("enabled") is not True:
                raise AcceptanceError("authorized_model_identity_invalid")
            if case.protocol not in item.get("capabilities", []) or case.operation not in item.get("operations", []):
                raise AcceptanceError("authorized_model_capability_invalid")
        return {
            "unauthenticated_models_status": 401,
            "authorized_model_count": len(listed),
            "model_ids": sorted(expected),
        }

    async def _wait_operation(self, operation_id: str) -> dict[str, Any]:
        while True:
            status, _, value, _ = await self._json(self.authorized, "GET", f"/v1/operations/{operation_id}")
            if status != 200 or not isinstance(value, dict):
                raise AcceptanceError("operation_poll_failed")
            if value.get("status") in _TERMINAL:
                return value
            await asyncio.sleep(min(1.0, self.remaining()))

    @staticmethod
    def _operation_summary(value: dict[str, Any], case: AcceptanceCase, operation_id: str) -> dict[str, object]:
        if (
            str(value.get("id")) != operation_id
            or value.get("model_id") != case.model_id
            or value.get("model_revision") != case.revision
            or value.get("protocol") != case.protocol
            or value.get("operation") != case.operation
            or value.get("status") != "succeeded"
            or value.get("semantic_outcome") != "protocol_valid"
        ):
            raise AcceptanceError("operation_terminal_identity_invalid")
        estimated = value.get("estimated_gpu_seconds")
        if not isinstance(estimated, int | float) or estimated <= 0:
            raise AcceptanceError("operation_accounting_invalid")
        return {
            "id": operation_id,
            "status": "succeeded",
            "attempt": value.get("attempt"),
            "estimated_gpu_seconds": estimated,
            "cold_start_seconds": value.get("cold_start_seconds"),
        }

    async def _http_case(self, case: AcceptanceCase, ordinal: int) -> dict[str, object]:
        started = time.monotonic()
        record: dict[str, object] = {
            "model_id": case.model_id,
            "model_revision": case.revision,
            "protocol": case.protocol,
            "operation": case.operation,
            "request_sha256": case.payload_sha256,
            "attempted": True,
        }
        try:
            path = "/v1/chat/completions" if case.protocol == "openai-chat" else f"/v1/models/{case.model_id}:invoke"
            request_payload: object = (
                case.payload
                if case.protocol.startswith("openai-")
                else {"operation": case.operation, "payload": case.payload}
            )
            idempotency = f"fs2-all-http-{self.nonce}-{ordinal}"
            status, headers, value, raw = await self._json(
                self.authorized,
                "POST",
                path,
                payload=request_payload,
                headers={
                    "idempotency-key": idempotency,
                    "x-fs2-wait-seconds": "30",
                    "x-fs2-deadline-seconds": str(max(1, int(self.remaining()))),
                },
            )
            if status not in {200, 202}:
                raise AcceptanceError("http_inference_admission_failed")
            operation_id = headers.get("x-fs2-operation-id")
            if operation_id is None:
                raise AcceptanceError("operation_header_missing")
            operation = await self._wait_operation(operation_id)
            operation_summary = self._operation_summary(operation, case, operation_id)
            if status == 202:
                result_status, _, value, raw = await self._json(
                    self.authorized, "GET", f"/v1/operations/{operation_id}/result"
                )
                if result_status != 200:
                    raise AcceptanceError("operation_result_failed")
            semantic = response_summary(value, raw, case.response_kind)
            record.update({"outcome": "pass", "operation_view": operation_summary, "semantic": semantic})
        except BaseException as error:  # noqa: BLE001 - suppress all value-bearing upstream errors
            record.update({"outcome": "fail", "failure_code": _safe_failure(error)})
        record["duration_seconds"] = round(time.monotonic() - started, 6)
        return record

    async def http_acceptance(self) -> list[dict[str, object]]:
        semaphore = asyncio.Semaphore(self.concurrency)

        async def bounded(case: AcceptanceCase, ordinal: int) -> dict[str, object]:
            async with semaphore:
                return await self._http_case(case, ordinal)

        return list(await asyncio.gather(*(bounded(case, index) for index, case in enumerate(self.cases, 1))))

    async def mcp_acceptance(self) -> list[dict[str, object]]:
        records: list[dict[str, object]] = []
        try:
            async with Client(
                streamable_http_client(f"{self.origin}/mcp", http_client=self.mcp_http),
                mode=MCP_PROTOCOL_VERSION,
            ) as client:
                if client.protocol_version != MCP_PROTOCOL_VERSION:
                    raise AcceptanceError("mcp_protocol_version_invalid")
                listed = await client.list_tools()
                if listed.ttl_ms != 0 or listed.cache_scope != "private":
                    raise AcceptanceError("mcp_discovery_cache_invalid")
                tools = discover_model_tools(listed.tools, self.cases)
                for ordinal, case in enumerate(self.cases, 1):
                    started = time.monotonic()
                    record: dict[str, object] = {
                        "model_id": case.model_id,
                        "model_revision": case.revision,
                        "protocol": case.protocol,
                        "operation": case.operation,
                        "request_sha256": case.payload_sha256,
                        "tool": tools[case.model_id],
                        "attempted": True,
                    }
                    try:
                        admitted = _mcp_result(
                            await client.call_tool(
                                tools[case.model_id],
                                {
                                    "payload": case.payload,
                                    "idempotency_key": f"fs2-all-mcp-{self.nonce}-{ordinal}",
                                    "wait_seconds": 0,
                                },
                            )
                        )
                        operation_id = admitted.get("id")
                        if not isinstance(operation_id, str):
                            raise AcceptanceError("mcp_operation_invalid")
                        current = admitted
                        while current.get("status") not in _TERMINAL:
                            await asyncio.sleep(min(1.0, self.remaining()))
                            current = _mcp_result(
                                await client.call_tool("get_operation", {"operation_id": operation_id})
                            )
                        operation = self._operation_summary(current, case, operation_id)
                        result = _mcp_result(
                            await client.call_tool("get_operation_result", {"operation_id": operation_id})
                        ).get("result")
                        raw = canonical_json(result)
                        semantic = response_summary(result, raw, case.response_kind)
                        record.update({"outcome": "pass", "operation_view": operation, "semantic": semantic})
                    except BaseException as error:  # noqa: BLE001
                        record.update({"outcome": "fail", "failure_code": _safe_failure(error)})
                    record["duration_seconds"] = round(time.monotonic() - started, 6)
                    records.append(record)
        except BaseException as error:  # noqa: BLE001
            failure = _safe_failure(error)
            for case in self.cases:
                records.append(
                    {
                        "model_id": case.model_id,
                        "model_revision": case.revision,
                        "protocol": case.protocol,
                        "operation": case.operation,
                        "request_sha256": case.payload_sha256,
                        "attempted": False,
                        "outcome": "fail",
                        "failure_code": failure,
                    }
                )
        return records

    async def run(self) -> dict[str, object]:
        evidence: dict[str, object] = {
            "schema": EVIDENCE_SCHEMA,
            "endpoint": self.origin,
            "tls_mode": self.tls_mode,
            "started_at": utc_now(),
            "release": {
                "release_id": self.release.release_id,
                "catalog_digest": self.release.catalog_digest,
                "inventory_digest": self.release.inventory_digest,
                "model_count": len(self.cases),
                "mcp_protocol_version": MCP_PROTOCOL_VERSION,
            },
        }
        try:
            evidence["preflight"] = await self.preflight()
        except BaseException as error:  # noqa: BLE001
            evidence.update(
                {"result": "FAIL", "preflight_failure_code": _safe_failure(error), "completed_at": utc_now()}
            )
            return evidence
        http_records = await self.http_acceptance()
        mcp_records = await self.mcp_acceptance()
        evidence["http"] = {
            "denominator": len(http_records),
            "passed": sum(item.get("outcome") == "pass" for item in http_records),
            "attempts": http_records,
        }
        evidence["mcp"] = {
            "denominator": len(mcp_records),
            "passed": sum(item.get("outcome") == "pass" for item in mcp_records),
            "private_zero_ttl_discovery": all(item.get("attempted") is True for item in mcp_records),
            "attempts": mcp_records,
        }
        evidence["result"] = (
            "PASS"
            if len(http_records) == len(self.cases)
            and len(mcp_records) == len(self.cases)
            and all(item.get("outcome") == "pass" for item in (*http_records, *mcp_records))
            else "FAIL"
        )
        evidence["completed_at"] = utc_now()
        return evidence


def write_evidence(path: Path, value: dict[str, object], *, forbidden: tuple[str, ...]) -> None:
    if not path.is_absolute():
        raise AcceptanceError("evidence_path_not_absolute")
    raw = json.dumps(value, indent=2, sort_keys=True, ensure_ascii=True) + "\n"
    if any(secret and secret in raw for secret in forbidden):
        raise AcceptanceError("evidence_redaction_failed")
    if path.exists() and (path.is_symlink() or not path.is_file()):
        raise AcceptanceError("evidence_path_invalid")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def parse_args(argv: list[str]) -> argparse.Namespace:
    project_root = Path(__file__).resolve().parents[3]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--endpoint", default="https://204.12.177.31")
    parser.add_argument("--token-file", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--catalog-root", type=Path, default=project_root / "catalog")
    parser.add_argument("--repo-root", type=Path, default=project_root.parents[1])
    parser.add_argument(
        "--inventory", type=Path, default=project_root / "control-plane/contracts/all-models-live-services.json"
    )
    parser.add_argument(
        "--cases", type=Path, default=project_root / "control-plane/contracts/all-models-live-acceptance.json"
    )
    parser.add_argument("--timeout-seconds", type=float, default=7200)
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument(
        "--tls-mode",
        choices=TLS_MODES,
        default=TLS_MODE_VERIFIED,
        help=(
            "TLS verification policy. disposable-staging-insecure is restricted to an explicit "
            "globally routable IPv4 endpoint and is only for the Terraform disposable staging gate."
        ),
    )
    args = parser.parse_args(argv)
    if not 300 <= args.timeout_seconds <= 14400:
        parser.error("--timeout-seconds must be in 300..14400")
    if not 1 <= args.concurrency <= 8:
        parser.error("--concurrency must be in 1..8")
    return args


async def _run(args: argparse.Namespace, token: str) -> dict[str, object]:
    catalog = load_catalog(args.catalog_root.resolve(), repo_root=args.repo_root.resolve())
    release = render_live_release(catalog, args.inventory.resolve())
    cases = _load_cases(args.cases.resolve(), catalog, release)
    runner = AcceptanceRunner(
        origin=args.endpoint,
        token=token,
        release=release,
        cases=cases,
        timeout_seconds=args.timeout_seconds,
        concurrency=args.concurrency,
        tls_mode=args.tls_mode,
    )
    try:
        return await runner.run()
    finally:
        await runner.close()


def main(argv: list[str] | None = None) -> int:
    logging.disable(logging.CRITICAL)
    args = parse_args(sys.argv[1:] if argv is None else argv)
    token = ""
    origin = "invalid"
    try:
        origin = validate_origin(args.endpoint)
        if args.token_file.resolve() == args.output.resolve():
            raise AcceptanceError("token_and_evidence_paths_match")
        token = read_token_file(args.token_file)
        evidence = asyncio.run(_run(args, token))
    except (AcceptanceError, CatalogError, LiveReleaseError) as error:
        evidence = {
            "schema": EVIDENCE_SCHEMA,
            "endpoint": origin,
            "tls_mode": args.tls_mode,
            "started_at": utc_now(),
            "completed_at": utc_now(),
            "result": "FAIL",
            "preflight_failure_code": error.code if isinstance(error, AcceptanceError) else "release_input_invalid",
        }
    write_evidence(args.output, evidence, forbidden=(token,))
    print(f"result={evidence['result']} evidence={args.output}")
    return 0 if evidence["result"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
