#!/usr/bin/env python3
"""Run one scientific model canary through only the public FS2 API.

The model-owned activation fragment supplies the request and exact supporting
input declarations.  This client deliberately ignores presigned handles and
uses the same authenticated gateway paths a customer can reach.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import ssl
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit
from urllib.request import Request, urlopen
from uuid import UUID, uuid4

RECEIPT_SCHEMA = "fs2-serve.nebius.ai/scientific-fleet-acceptance-receipt/v1"
REQUEST_SCHEMA = "fs2-serve.nebius.ai/scientific-run-request/v1"
MANIFEST_SCHEMA = "fs2-serve.nebius.ai/scientific-artifact-manifest/v1"
MANIFEST_MEDIA_TYPE = "application/vnd.fs2.scientific-manifest+json"
RESULT_SCHEMA = "fs2-serve.nebius.ai/scientific-run-result/v1"
MAX_JSON_BYTES = 16 * 1024 * 1024
MAX_INPUT_BYTES = 256 * 1024 * 1024
SHA256_RE = re.compile(r"^[a-f0-9]{64}$")
MODEL_RE = re.compile(r"^[a-z0-9](?:[-a-z0-9]*[a-z0-9])?$")
SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
MEDIA_TYPE_RE = re.compile(r"^[a-z0-9][a-z0-9.+-]*/[A-Za-z0-9][A-Za-z0-9.+_-]*$")
TERMINAL = frozenset({"succeeded", "failed", "cancelled"})
SENSITIVE_TEXT_RE = re.compile(
    r"(?:bearer\s+|x-amz-(?:algorithm|credential|signature|security-token)|aws_access_key_id|"
    r"set-cookie\s*:|authorization\s*:)",
    re.IGNORECASE,
)


class AcceptanceError(RuntimeError):
    """A stable, non-secret failure code for one fail-closed boundary."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class HttpResponse:
    status: int
    headers: dict[str, str]
    body: bytes


@dataclass(frozen=True, slots=True)
class RunConfig:
    endpoint: str
    repository_root: Path
    activation_fragment: Path
    receipt_path: Path
    run_id: str
    timeout_seconds: float = 7200.0
    poll_seconds: float = 5.0
    request_timeout_seconds: float = 60.0
    overwrite: bool = False


@dataclass(frozen=True, slots=True)
class DeclaredInput:
    role: str
    name: str | None
    data: bytes
    path: Path
    encoding: str


class PublicApiClient:
    """Small bounded HTTP client that never returns auth material in errors."""

    def __init__(
        self, endpoint: str, token: str, *, timeout_seconds: float = 60.0
    ) -> None:
        parsed = urlsplit(endpoint)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.netloc
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or parsed.path not in {"", "/"}
        ):
            raise AcceptanceError("endpoint_invalid")
        if not token or any(character.isspace() for character in token):
            raise AcceptanceError("token_invalid")
        if not 0 < timeout_seconds <= 600:
            raise AcceptanceError("request_timeout_invalid")
        self.endpoint = f"{parsed.scheme}://{parsed.netloc}"
        self.host = parsed.netloc
        self.tls = parsed.scheme == "https"
        self._token = token
        self._timeout_seconds = timeout_seconds
        self._ssl_context = ssl.create_default_context() if self.tls else None

    def request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        body: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> HttpResponse:
        if not path.startswith("/") or path.startswith("//"):
            raise AcceptanceError("http_path_invalid")
        if json_body is not None and body is not None:
            raise AcceptanceError("http_body_ambiguous")
        request_headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {self._token}",
        }
        request_body = body
        if json_body is not None:
            request_body = _canonical_json(json_body)
            request_headers["Content-Type"] = "application/json"
        if headers:
            request_headers.update(headers)
        request = Request(
            f"{self.endpoint}{path}",
            data=request_body,
            headers=request_headers,
            method=method,
        )
        try:
            with urlopen(  # noqa: S310 - endpoint is operator supplied and validated above.
                request,
                timeout=self._timeout_seconds,
                context=self._ssl_context,
            ) as response:
                payload = response.read(MAX_JSON_BYTES + 1)
                if len(payload) > MAX_JSON_BYTES:
                    raise AcceptanceError("http_response_too_large")
                return HttpResponse(
                    status=response.status,
                    headers={
                        key.lower(): value for key, value in response.headers.items()
                    },
                    body=payload,
                )
        except HTTPError as error:
            payload = error.read(MAX_JSON_BYTES + 1)
            if len(payload) > MAX_JSON_BYTES:
                payload = b""
            return HttpResponse(
                status=error.code,
                headers={key.lower(): value for key, value in error.headers.items()},
                body=payload,
            )
        except (TimeoutError, URLError, OSError) as error:
            raise AcceptanceError("http_transport_failed") from error


def _canonical_json(value: object, *, newline: bool = False) -> bytes:
    suffix = "\n" if newline else ""
    try:
        return (
            json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
            + suffix
        ).encode()
    except (TypeError, ValueError) as error:
        raise AcceptanceError("json_not_canonicalizable") from error


def _object(value: object, code: str) -> dict[str, Any]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise AcceptanceError(code)
    return value


def _items(value: object, code: str) -> list[Any]:
    if not isinstance(value, list):
        raise AcceptanceError(code)
    return value


def _json_response(
    response: HttpResponse, expected_status: int, action: str
) -> dict[str, Any]:
    if response.status != expected_status:
        raise AcceptanceError(f"http_{action}_{response.status}")
    try:
        value = json.loads(response.body)
    except (RecursionError, UnicodeDecodeError, ValueError) as error:
        raise AcceptanceError(f"{action}_response_invalid") from error
    return _object(value, f"{action}_response_invalid")


def _load_json(path: Path, code: str) -> dict[str, Any]:
    try:
        raw = path.read_bytes()
        if len(raw) > MAX_JSON_BYTES:
            raise AcceptanceError(code)
        return _object(json.loads(raw), code)
    except (OSError, RecursionError, UnicodeDecodeError, ValueError) as error:
        raise AcceptanceError(code) from error


def _resolve_inside(root: Path, value: object, code: str) -> Path:
    if not isinstance(value, str) or not value or Path(value).is_absolute():
        raise AcceptanceError(code)
    try:
        resolved_root = root.resolve(strict=True)
        resolved = (resolved_root / value).resolve(strict=True)
        resolved.relative_to(resolved_root)
    except (OSError, ValueError) as error:
        raise AcceptanceError(code) from error
    if not resolved.is_file():
        raise AcceptanceError(code)
    return resolved


def _read_declared_input(root: Path, value: object) -> DeclaredInput:
    declaration = _object(value, "supporting_input_invalid")
    if set(declaration) - {"role", "name", "path", "encoding"}:
        raise AcceptanceError("supporting_input_invalid")
    role = declaration.get("role")
    name = declaration.get("name")
    encoding = declaration.get("encoding")
    if role not in {"request-input-manifest", "manifest-artifact"}:
        raise AcceptanceError("supporting_input_role_invalid")
    if name is not None and (
        not isinstance(name, str) or SAFE_ID_RE.fullmatch(name) is None
    ):
        raise AcceptanceError("supporting_input_name_invalid")
    if encoding not in {"raw", "canonical-json", "canonical-json-newline"}:
        raise AcceptanceError("supporting_input_encoding_invalid")
    path = _resolve_inside(
        root, declaration.get("path"), "supporting_input_path_invalid"
    )
    try:
        data = path.read_bytes()
    except OSError as error:
        raise AcceptanceError("supporting_input_unavailable") from error
    if len(data) > MAX_INPUT_BYTES:
        raise AcceptanceError("supporting_input_too_large")
    if encoding != "raw":
        try:
            document = json.loads(data)
        except (RecursionError, UnicodeDecodeError, ValueError) as error:
            raise AcceptanceError("supporting_input_json_invalid") from error
        data = _canonical_json(document, newline=encoding == "canonical-json-newline")
    return DeclaredInput(role=role, name=name, data=data, path=path, encoding=encoding)


def _artifact_ref(value: object, code: str) -> dict[str, Any]:
    pointer = _object(value, code)
    if set(pointer) - {
        "artifact_id",
        "sha256",
        "size_bytes",
        "media_type",
        "compression",
    }:
        raise AcceptanceError(code)
    artifact_id = pointer.get("artifact_id")
    sha256 = pointer.get("sha256")
    size_bytes = pointer.get("size_bytes")
    media_type = pointer.get("media_type")
    compression = pointer.get("compression", "none")
    if not isinstance(artifact_id, str) or SAFE_ID_RE.fullmatch(artifact_id) is None:
        raise AcceptanceError(code)
    if not isinstance(sha256, str) or SHA256_RE.fullmatch(sha256) is None:
        raise AcceptanceError(code)
    if (
        not isinstance(size_bytes, int)
        or isinstance(size_bytes, bool)
        or size_bytes < 0
    ):
        raise AcceptanceError(code)
    if (
        not isinstance(media_type, str)
        or len(media_type) > 128
        or MEDIA_TYPE_RE.fullmatch(media_type) is None
    ):
        raise AcceptanceError(code)
    if compression not in {"none", "gzip", "zstd"}:
        raise AcceptanceError(code)
    return {
        "artifact_id": artifact_id,
        "sha256": sha256,
        "size_bytes": size_bytes,
        "media_type": media_type,
        "compression": compression,
    }


def _measured_ref(
    data: bytes, *, artifact_id: str, media_type: str, compression: str
) -> dict[str, Any]:
    return {
        "artifact_id": artifact_id,
        "sha256": hashlib.sha256(data).hexdigest(),
        "size_bytes": len(data),
        "media_type": media_type,
        "compression": compression,
    }


def _verify_declared_bytes(pointer: dict[str, Any], data: bytes) -> None:
    if pointer["sha256"] != hashlib.sha256(data).hexdigest():
        raise AcceptanceError("declared_digest_mismatch")
    if pointer["size_bytes"] != len(data):
        raise AcceptanceError("declared_size_mismatch")


def _uuid(value: object, code: str) -> str:
    try:
        return str(UUID(str(value)))
    except (TypeError, ValueError) as error:
        raise AcceptanceError(code) from error


def _idempotency_key(run_id: str, purpose: str, digest: str) -> str:
    identity = hashlib.sha256(f"{run_id}\0{purpose}\0{digest}".encode()).hexdigest()
    return f"fs2-scientific-acceptance-{identity}"


def _content_path(value: object, *, operation_id: str, upload_id: str) -> str:
    if not isinstance(value, str):
        raise AcceptanceError("upload_content_path_invalid")
    parsed = urlsplit(value)
    expected_path = f"/v1/scientific-artifacts/uploads/{upload_id}/content"
    query = parse_qs(parsed.query, strict_parsing=True)
    if (
        parsed.scheme
        or parsed.netloc
        or parsed.fragment
        or parsed.path != expected_path
        or query != {"operation_id": [operation_id]}
    ):
        raise AcceptanceError("upload_content_path_invalid")
    return value


def _upload(
    client: PublicApiClient,
    *,
    model_id: str,
    data: bytes,
    media_type: str,
    compression: str,
    idempotency_key: str,
) -> dict[str, Any]:
    measured = _measured_ref(
        data, artifact_id="pending", media_type=media_type, compression=compression
    )
    begun = _json_response(
        client.request(
            "POST",
            "/v1/scientific-artifacts/uploads",
            json_body={
                "model_id": model_id,
                "sha256": measured["sha256"],
                "size_bytes": measured["size_bytes"],
                "media_type": media_type,
                "compression": compression,
            },
            headers={"Idempotency-Key": idempotency_key},
        ),
        201,
        "upload_begin",
    )
    operation_id = _uuid(begun.get("operation_id"), "upload_operation_id_invalid")
    upload_id = _uuid(begun.get("upload_id"), "upload_id_invalid")
    max_bytes = begun.get("max_content_bytes")
    if (
        not isinstance(max_bytes, int)
        or isinstance(max_bytes, bool)
        or max_bytes < len(data)
    ):
        raise AcceptanceError("upload_size_not_supported")
    content_path = _content_path(
        begun.get("content_path"), operation_id=operation_id, upload_id=upload_id
    )
    stored = _json_response(
        client.request(
            "PUT",
            content_path,
            body=data,
            headers={"Content-Type": media_type, "Content-Length": str(len(data))},
        ),
        200,
        "upload_content",
    )
    if (
        _uuid(stored.get("operation_id"), "upload_receipt_mismatch") != operation_id
        or _uuid(stored.get("upload_id"), "upload_receipt_mismatch") != upload_id
        or stored.get("sha256") != measured["sha256"]
        or stored.get("size_bytes") != len(data)
        or stored.get("media_type") != media_type
        or stored.get("finalized") is not False
    ):
        raise AcceptanceError("upload_receipt_mismatch")
    finalized = _artifact_ref(
        _json_response(
            client.request(
                "POST",
                f"/v1/scientific-artifacts/uploads/{upload_id}:finalize",
                json_body={"operation_id": operation_id},
            ),
            200,
            "upload_finalize",
        ),
        "finalized_artifact_invalid",
    )
    if any(
        finalized[field] != measured[field]
        for field in ("sha256", "size_bytes", "media_type", "compression")
    ):
        raise AcceptanceError("finalized_artifact_mismatch")
    return finalized


def _activation(
    config: RunConfig,
) -> tuple[str, dict[str, Any], list[DeclaredInput], dict[str, Any]]:
    try:
        root = config.repository_root.resolve(strict=True)
    except OSError as error:
        raise AcceptanceError("repository_root_invalid") from error
    fragment_path = config.activation_fragment
    if not fragment_path.is_absolute():
        fragment_path = root / fragment_path
    try:
        fragment_path = fragment_path.resolve(strict=True)
        fragment_path.relative_to(root)
    except (OSError, ValueError) as error:
        raise AcceptanceError("activation_fragment_invalid") from error
    fragment = _load_json(fragment_path, "activation_fragment_invalid")
    model_id = fragment.get("model_id")
    if (
        not isinstance(model_id, str)
        or len(model_id) > 63
        or MODEL_RE.fullmatch(model_id) is None
    ):
        raise AcceptanceError("activation_model_invalid")
    fixtures = _object(fragment.get("public_fixtures"), "activation_fixtures_invalid")
    request_path = _resolve_inside(
        root, fixtures.get("request"), "activation_request_path_invalid"
    )
    request = _load_json(request_path, "activation_request_invalid")
    if request.get("schema") != REQUEST_SCHEMA:
        raise AcceptanceError("activation_request_schema_invalid")
    declarations = [
        _read_declared_input(root, item)
        for item in _items(
            fixtures.get("supporting_inputs"), "supporting_inputs_missing"
        )
    ]
    if not declarations:
        raise AcceptanceError("supporting_inputs_missing")
    return model_id, request, declarations, fragment


def _entry_inputs(
    template: dict[str, Any], declarations: list[DeclaredInput]
) -> list[tuple[dict[str, Any], DeclaredInput]]:
    if template.get("schema") != MANIFEST_SCHEMA:
        raise AcceptanceError("input_manifest_schema_invalid")
    entries = _items(template.get("entries"), "input_manifest_entries_invalid")
    if not entries or len(entries) > 10_000:
        raise AcceptanceError("input_manifest_entries_invalid")
    if len(entries) != len(declarations):
        raise AcceptanceError("manifest_artifact_count_mismatch")
    remaining = list(declarations)
    matched: list[tuple[dict[str, Any], DeclaredInput]] = []
    names: set[str] = set()
    for raw_entry in entries:
        entry = _object(raw_entry, "input_manifest_entry_invalid")
        name = entry.get("name")
        semantic_type = entry.get("semantic_type")
        if (
            not isinstance(name, str)
            or SAFE_ID_RE.fullmatch(name) is None
            or name in names
        ):
            raise AcceptanceError("input_manifest_entry_invalid")
        if not isinstance(semantic_type, str) or "/v" not in semantic_type:
            raise AcceptanceError("input_manifest_entry_invalid")
        names.add(name)
        pointer = _artifact_ref(
            entry.get("artifact"), "input_manifest_artifact_invalid"
        )
        candidates = [item for item in remaining if item.name == name]
        if not candidates:
            candidates = [
                item
                for item in remaining
                if item.name is None
                if hashlib.sha256(item.data).hexdigest() == pointer["sha256"]
                and len(item.data) == pointer["size_bytes"]
            ]
        if len(candidates) != 1:
            raise AcceptanceError("manifest_artifact_binding_ambiguous")
        declared = candidates[0]
        _verify_declared_bytes(pointer, declared.data)
        remaining.remove(declared)
        matched.append((entry, declared))
    if remaining:
        raise AcceptanceError("manifest_artifact_unmatched")
    return matched


def _prepare_input(
    client: PublicApiClient,
    *,
    model_id: str,
    request: dict[str, Any],
    declarations: list[DeclaredInput],
    run_id: str,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    request_pointer = _artifact_ref(
        request.get("input_manifest"), "request_input_artifact_invalid"
    )
    request_inputs = [
        item for item in declarations if item.role == "request-input-manifest"
    ]
    artifact_inputs = [
        item for item in declarations if item.role == "manifest-artifact"
    ]
    if len(request_inputs) != 1:
        raise AcceptanceError("request_input_declaration_invalid")
    root_input = request_inputs[0]
    _verify_declared_bytes(request_pointer, root_input.data)
    evidence: list[dict[str, Any]] = []

    if request_pointer["media_type"] != MANIFEST_MEDIA_TYPE:
        if artifact_inputs:
            raise AcceptanceError("direct_input_has_manifest_artifacts")
        uploaded = _upload(
            client,
            model_id=model_id,
            data=root_input.data,
            media_type=request_pointer["media_type"],
            compression=request_pointer["compression"],
            idempotency_key=_idempotency_key(
                run_id, "direct-input", request_pointer["sha256"]
            ),
        )
        evidence.append({"role": "request-input", **uploaded})
        request["input_manifest"] = uploaded
        return request, evidence

    if root_input.encoding not in {"canonical-json", "canonical-json-newline"}:
        raise AcceptanceError("manifest_encoding_invalid")
    try:
        manifest = _object(json.loads(root_input.data), "input_manifest_invalid")
    except (RecursionError, UnicodeDecodeError, ValueError) as error:
        raise AcceptanceError("input_manifest_invalid") from error
    for entry, declared in _entry_inputs(manifest, artifact_inputs):
        pointer = _artifact_ref(entry["artifact"], "input_manifest_artifact_invalid")
        uploaded = _upload(
            client,
            model_id=model_id,
            data=declared.data,
            media_type=pointer["media_type"],
            compression=pointer["compression"],
            idempotency_key=_idempotency_key(
                run_id, f"manifest-entry:{entry['name']}", pointer["sha256"]
            ),
        )
        entry["artifact"] = uploaded
        evidence.append(
            {"role": "manifest-artifact", "name": entry["name"], **uploaded}
        )
    manifest_bytes = _canonical_json(
        manifest, newline=root_input.encoding == "canonical-json-newline"
    )
    manifest_pointer = _upload(
        client,
        model_id=model_id,
        data=manifest_bytes,
        media_type=MANIFEST_MEDIA_TYPE,
        compression=request_pointer["compression"],
        idempotency_key=_idempotency_key(
            run_id,
            "input-manifest",
            hashlib.sha256(manifest_bytes).hexdigest(),
        ),
    )
    evidence.append({"role": "request-input-manifest", **manifest_pointer})
    request["input_manifest"] = manifest_pointer
    return request, evidence


def _operation_identity(
    status: dict[str, Any], *, operation_id: str, model_id: str, operation: str
) -> None:
    operation_view = _object(status.get("operation"), "operation_status_invalid")
    batch = _object(status.get("batch"), "batch_status_invalid")
    if (
        _uuid(operation_view.get("id"), "operation_identity_mismatch") != operation_id
        or operation_view.get("model_id") != model_id
        or operation_view.get("operation") != operation
        or batch.get("model_id") != model_id
    ):
        raise AcceptanceError("operation_identity_mismatch")


def _submit(
    client: PublicApiClient,
    *,
    model_id: str,
    request: dict[str, Any],
    run_id: str,
) -> tuple[str, dict[str, Any]]:
    operation = request.get("operation")
    if not isinstance(operation, str) or not operation:
        raise AcceptanceError("request_operation_invalid")
    request_digest = hashlib.sha256(_canonical_json(request)).hexdigest()
    submitted_response = client.request(
        "POST",
        f"/v1/models/{model_id}:submit",
        json_body=request,
        headers={"Idempotency-Key": _idempotency_key(run_id, "submit", request_digest)},
    )
    submitted = _json_response(submitted_response, 202, "submit")
    operation_id = _uuid(
        _object(submitted.get("operation"), "submit_response_invalid").get("id"),
        "submit_operation_id_invalid",
    )
    header_id = submitted_response.headers.get("x-fs2-operation-id")
    if (
        header_id is not None
        and _uuid(header_id, "submit_operation_id_mismatch") != operation_id
    ):
        raise AcceptanceError("submit_operation_id_mismatch")
    location = submitted_response.headers.get("location")
    if location is not None and location != f"/v1/operations/{operation_id}":
        raise AcceptanceError("submit_location_mismatch")
    _operation_identity(
        submitted, operation_id=operation_id, model_id=model_id, operation=operation
    )
    return operation_id, submitted


def _poll(
    client: PublicApiClient,
    *,
    operation_id: str,
    model_id: str,
    operation: str,
    initial: dict[str, Any],
    timeout_seconds: float,
    poll_seconds: float,
) -> dict[str, Any]:
    if timeout_seconds < 0 or poll_seconds <= 0:
        raise AcceptanceError("poll_configuration_invalid")
    deadline = time.monotonic() + timeout_seconds
    status = initial
    while True:
        _operation_identity(
            status, operation_id=operation_id, model_id=model_id, operation=operation
        )
        operation_state = _object(status["operation"], "operation_status_invalid").get(
            "status"
        )
        batch_state = _object(status["batch"], "batch_status_invalid").get("status")
        if operation_state in TERMINAL or batch_state in TERMINAL:
            if operation_state != "succeeded" or batch_state != "succeeded":
                raise AcceptanceError("operation_terminal_failure")
            return status
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise AcceptanceError("operation_timeout")
        time.sleep(min(poll_seconds, remaining))
        status = _json_response(
            client.request("GET", f"/v1/operations/{operation_id}"),
            200,
            "operation_status",
        )


def _expected_execution(fragment: dict[str, Any]) -> tuple[str | None, dict[str, Any]]:
    projection = _object(
        fragment.get("profile_projection"), "activation_profile_projection_invalid"
    )
    profile = _object(projection.get("profile"), "activation_profile_invalid")
    identity = _object(
        profile.get("execution_identity"), "activation_execution_identity_invalid"
    )
    execution = _object(
        fragment.get("execution_projection"), "activation_execution_projection_invalid"
    )
    variant_id = execution.get("variant_id")
    if variant_id is not None and not isinstance(variant_id, str):
        raise AcceptanceError("activation_variant_invalid")
    return variant_id, identity


def _validate_result(
    value: dict[str, Any],
    *,
    status: dict[str, Any],
    operation_id: str,
    model_id: str,
    input_pointer: dict[str, Any],
    fragment: dict[str, Any],
) -> None:
    operation = _object(status.get("operation"), "operation_status_invalid")
    batch = _object(status.get("batch"), "batch_status_invalid")
    semantic = _object(value.get("semantic_validation"), "semantic_validation_invalid")
    execution = _object(value.get("execution_identity"), "execution_identity_invalid")
    expected_variant, expected_identity = _expected_execution(fragment)
    if value.get("schema") != RESULT_SCHEMA:
        raise AcceptanceError("result_schema_invalid")
    if (
        value.get("operation_id") != operation_id
        or value.get("batch_id") != batch.get("batch_id")
        or value.get("workload_id") != batch.get("workload_id")
    ):
        raise AcceptanceError("result_identity_mismatch")
    if value.get("terminal_status") != "succeeded" or value.get("error") is not None:
        raise AcceptanceError("result_terminal_failure")
    if semantic.get("status") != "passed" or not isinstance(
        semantic.get("receipt_digest"), str
    ):
        raise AcceptanceError("semantic_validation_failed")
    if SHA256_RE.fullmatch(semantic["receipt_digest"]) is None:
        raise AcceptanceError("semantic_validation_invalid")
    if (
        _artifact_ref(value.get("input_manifest"), "result_input_artifact_invalid")
        != input_pointer
    ):
        raise AcceptanceError("result_input_artifact_mismatch")
    _artifact_ref(value.get("output_manifest"), "result_output_artifact_invalid")
    attempts = _items(value.get("attempts"), "result_attempts_invalid")
    if not attempts:
        raise AcceptanceError("result_attempts_invalid")
    if (
        execution.get("model_id") != model_id
        or execution.get("variant_id") != batch.get("variant_id")
        or execution.get("model_revision") != operation.get("model_revision")
    ):
        raise AcceptanceError("execution_identity_mismatch")
    if expected_variant is not None and execution.get("variant_id") != expected_variant:
        raise AcceptanceError("execution_identity_mismatch")
    expected_to_result = {
        "model_revision": "model_revision",
        "runtime_image_digest": "runtime_image_digest",
        "runtime_recipe_sha256": "runtime_recipe_sha256",
        "workload_recipe_sha256": "workload_recipe_sha256",
        "artifact_manifest_digest": "model_artifact_manifest_digest",
        "execution_identity_sha256": "execution_identity_sha256",
    }
    for expected_key, result_key in expected_to_result.items():
        expected = expected_identity.get(expected_key)
        if expected is not None and execution.get(result_key) != expected:
            raise AcceptanceError("execution_identity_mismatch")
    if (
        operation.get("semantic_outcome") != "passed"
        or batch.get("result_published") is not True
    ):
        raise AcceptanceError("semantic_validation_failed")


def _select(value: object, fields: tuple[str, ...], code: str) -> dict[str, Any]:
    source = _object(value, code)
    return {field: source.get(field) for field in fields}


def _scheduling_admission(value: object, code: str) -> dict[str, Any] | None:
    if value is None:
        return None
    return _select(
        value,
        (
            "resolved_pool_id",
            "admitted_resource_flavor",
            "accelerator_resource_name",
            "accelerator_count",
            "admitted_at",
        ),
        code,
    )


def _receipt(
    *,
    client: PublicApiClient,
    model_id: str,
    status: dict[str, Any],
    result: dict[str, Any],
    uploads: list[dict[str, Any]],
) -> dict[str, Any]:
    operation = _object(status["operation"], "operation_status_invalid")
    batch = _object(status["batch"], "batch_status_invalid")
    scheduling = _object(result["scheduling_snapshot"], "result_scheduling_invalid")
    semantic = _object(result["semantic_validation"], "semantic_validation_invalid")
    observed_stages: list[dict[str, Any]] = []
    for raw_stage in _items(batch.get("stages"), "batch_stages_invalid"):
        stage = _object(raw_stage, "batch_stage_invalid")
        attempts: list[dict[str, Any]] = []
        for raw_attempt in _items(stage.get("attempts"), "batch_attempts_invalid"):
            attempt = _select(
                raw_attempt,
                (
                    "attempt_id",
                    "shard_id",
                    "attempt_number",
                    "workload_kind",
                    "workload_name",
                    "workload_uid",
                    "workload_namespace",
                    "route_namespace",
                    "outcome",
                    "last_phase",
                    "resource_released",
                    "failure_kind",
                    "failure_code",
                ),
                "batch_attempt_invalid",
            )
            admission = _object(raw_attempt, "batch_attempt_invalid").get(
                "scheduling_admission"
            )
            attempt["scheduling_admission"] = _scheduling_admission(
                admission, "batch_admission_invalid"
            )
            attempts.append(attempt)
        observed_stages.append(
            {
                **_select(
                    stage, ("stage_id", "status", "failure_code"), "batch_stage_invalid"
                ),
                "attempts": attempts,
            }
        )
    result_attempts: list[dict[str, Any]] = []
    for raw_attempt in _items(result.get("attempts"), "result_attempts_invalid"):
        attempt = _select(
            raw_attempt,
            (
                "attempt_id",
                "stage_id",
                "shard_id",
                "attempt_number",
                "status",
                "started_at",
                "completed_at",
                "scheduling_admission",
                "kueue_workload_uid",
                "k8s_job_uid",
                "pod_uids",
                "node_uids",
                "gpu_uuids",
                "checkpoint_input",
                "checkpoint_output",
            ),
            "result_attempt_invalid",
        )
        for field in ("checkpoint_input", "checkpoint_output"):
            if attempt[field] is not None:
                attempt[field] = _artifact_ref(
                    attempt[field], "result_checkpoint_invalid"
                )
        attempt["scheduling_admission"] = _scheduling_admission(
            attempt["scheduling_admission"], "result_admission_invalid"
        )
        result_attempts.append(attempt)
    stage_decisions = [
        _select(
            item,
            (
                "stage_id",
                "resource_class",
                "resolved_cluster_queue",
                "resolved_local_queue",
                "workload_priority_class",
                "workload_priority_value",
                "resolved_pool_preference",
                "accelerator_resource_name",
                "accelerator_count",
                "max_queue_seconds",
                "max_execution_seconds",
                "checkpoint_mode",
                "preemption_mode",
            ),
            "scheduling_stage_invalid",
        )
        for item in _items(scheduling.get("stages"), "scheduling_stages_invalid")
    ]
    receipt = {
        "schema": RECEIPT_SCHEMA,
        "endpoint": {"host": client.host, "tls": client.tls},
        "model": {"model_id": model_id, "variant_id": batch.get("variant_id")},
        "operation_identity": _select(
            result,
            ("operation_id", "batch_id", "workload_id"),
            "result_identity_invalid",
        ),
        "terminal_state": {
            "operation": operation.get("status"),
            "batch": batch.get("status"),
            "result": result.get("terminal_status"),
            "semantic_validation": semantic.get("status"),
        },
        "timestamps": {
            **_select(
                operation,
                (
                    "accepted_at",
                    "available_at",
                    "activation_started_at",
                    "ready_at",
                    "started_at",
                    "completed_at",
                ),
                "operation_timestamps_invalid",
            ),
            "result_submitted_at": result.get("submitted_at"),
            "result_completed_at": result.get("completed_at"),
        },
        "cold_start": {
            "cold_start_seconds": operation.get("cold_start_seconds"),
            "runtime": _select(
                operation.get("runtime"),
                ("pod_uid", "node_uid", "gpu_uuids", "gpu_count", "preemptible"),
                "operation_runtime_invalid",
            ),
        },
        "execution_identity": _select(
            result.get("execution_identity"),
            (
                "model_id",
                "variant_id",
                "model_revision",
                "runtime_image_digest",
                "runtime_recipe_sha256",
                "workload_recipe_sha256",
                "model_artifact_manifest_digest",
                "execution_identity_sha256",
            ),
            "execution_identity_invalid",
        ),
        "queue": {
            "scheduling_snapshot_digest": batch.get("scheduling_snapshot_digest"),
            "policy_revision": scheduling.get("policy_revision"),
            "captured_at": scheduling.get("captured_at"),
            "service_class": scheduling.get("service_class"),
            "tenant_queue": scheduling.get("tenant_queue"),
            "model_lane": scheduling.get("model_lane"),
            "stage_decisions": stage_decisions,
            "observed_stages": observed_stages,
        },
        "attempts": result_attempts,
        "artifact_digests": {
            "uploads": sorted(
                uploads,
                key=lambda item: (str(item.get("role")), str(item.get("name", ""))),
            ),
            "input_manifest": _artifact_ref(
                result.get("input_manifest"), "result_input_artifact_invalid"
            ),
            "output_manifest": _artifact_ref(
                result.get("output_manifest"), "result_output_artifact_invalid"
            ),
            "semantic_validation_receipt_sha256": semantic.get("receipt_digest"),
        },
    }
    _assert_redacted(receipt)
    return receipt


def _assert_redacted(value: object) -> None:
    if isinstance(value, dict):
        forbidden = re.compile(
            r"(?:token|password|secret|credential|cookie|authorization|signed_url|storage_key)",
            re.I,
        )
        for key, item in value.items():
            if forbidden.search(str(key)):
                raise AcceptanceError("receipt_redaction_failed")
            _assert_redacted(item)
    elif isinstance(value, list):
        for item in value:
            _assert_redacted(item)
    elif isinstance(value, str) and SENSITIVE_TEXT_RE.search(value):
        raise AcceptanceError("receipt_redaction_failed")


def _write_receipt(path: Path, receipt: dict[str, Any], *, overwrite: bool) -> None:
    body = (
        json.dumps(receipt, sort_keys=True, indent=2, allow_nan=False).encode() + b"\n"
    )
    parent = path.parent
    parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and not overwrite:
        raise AcceptanceError("receipt_exists")
    temporary: Path | None = None
    try:
        descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=parent)
        temporary = Path(name)
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(body)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except OSError as error:
        raise AcceptanceError("receipt_write_failed") from error
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def run_acceptance(config: RunConfig, client: PublicApiClient) -> dict[str, Any]:
    model_id, request, declarations, fragment = _activation(config)
    request, uploads = _prepare_input(
        client,
        model_id=model_id,
        request=request,
        declarations=declarations,
        run_id=config.run_id,
    )
    input_pointer = _artifact_ref(
        request["input_manifest"], "request_input_artifact_invalid"
    )
    operation_id, submitted = _submit(
        client, model_id=model_id, request=request, run_id=config.run_id
    )
    operation_name = str(request["operation"])
    terminal = _poll(
        client,
        operation_id=operation_id,
        model_id=model_id,
        operation=operation_name,
        initial=submitted,
        timeout_seconds=config.timeout_seconds,
        poll_seconds=config.poll_seconds,
    )
    result = _json_response(
        client.request("GET", f"/v1/operations/{operation_id}/result"),
        200,
        "operation_result",
    )
    _validate_result(
        result,
        status=terminal,
        operation_id=operation_id,
        model_id=model_id,
        input_pointer=input_pointer,
        fragment=fragment,
    )
    receipt = _receipt(
        client=client,
        model_id=model_id,
        status=terminal,
        result=result,
        uploads=uploads,
    )
    _write_receipt(config.receipt_path, receipt, overwrite=config.overwrite)
    return receipt


def _arguments(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--endpoint",
        required=True,
        help="Public FS2 origin, with no path or credentials",
    )
    parser.add_argument(
        "--token-env",
        default="FS2_INFERENCE_TOKEN",
        help="Environment variable containing the bearer token (default: FS2_INFERENCE_TOKEN)",
    )
    parser.add_argument(
        "--repository-root",
        type=Path,
        default=Path(__file__).resolve().parents[2],
        help="k8s-inference directory containing fragment-relative paths",
    )
    parser.add_argument("--activation-fragment", type=Path, required=True)
    parser.add_argument("--receipt", type=Path, required=True)
    parser.add_argument(
        "--run-id", default=None, help="Opaque run identity; defaults to a fresh UUID"
    )
    parser.add_argument("--timeout-seconds", type=float, default=7200.0)
    parser.add_argument("--poll-seconds", type=float, default=5.0)
    parser.add_argument("--request-timeout-seconds", type=float, default=60.0)
    parser.add_argument("--overwrite", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    arguments = _arguments(argv)
    token = os.environ.get(arguments.token_env)
    if token is None:
        print(
            "scientific acceptance failed: token_environment_missing", file=sys.stderr
        )
        return 2
    run_id = arguments.run_id or str(uuid4())
    if SAFE_ID_RE.fullmatch(run_id) is None:
        print("scientific acceptance failed: run_id_invalid", file=sys.stderr)
        return 2
    config = RunConfig(
        endpoint=arguments.endpoint,
        repository_root=arguments.repository_root,
        activation_fragment=arguments.activation_fragment,
        receipt_path=arguments.receipt,
        run_id=run_id,
        timeout_seconds=arguments.timeout_seconds,
        poll_seconds=arguments.poll_seconds,
        request_timeout_seconds=arguments.request_timeout_seconds,
        overwrite=arguments.overwrite,
    )
    try:
        client = PublicApiClient(
            config.endpoint,
            token,
            timeout_seconds=config.request_timeout_seconds,
        )
        receipt = run_acceptance(config, client)
    except AcceptanceError as error:
        print(f"scientific acceptance failed: {error.code}", file=sys.stderr)
        return 1
    print(
        json.dumps(
            {
                "receipt": str(config.receipt_path),
                "operation_id": receipt["operation_identity"]["operation_id"],
                "status": "succeeded",
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
