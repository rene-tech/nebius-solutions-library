"""Typed native dispatch and owner-scoped artifact transport, without a scheduler.

Call ``dispatch_native`` inside the gateway's authenticated request scope. Its
injected callable MUST use that request's original S2 key and exact App binding.
No endpoint URL, bucket, object key, or native deployment is customer input.
Only S2 supplies operation identity and admission state; this module never
retries a submission or claims synchronous invocation is idempotent.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import io
import json
import math
from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from botocore.exceptions import ClientError
from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError
from mcp.server.mcpserver.utilities.func_metadata import FuncMetadata
from mcp.shared.exceptions import MCPError
from mcp_types import INVALID_PARAMS

from .endpoint_catalog import EndpointContract
from .gateway_store import GatewayStore, canonical, identifier, uuid

_TRANSPORT = "x-fs2-artifact-materialization"
_POINTER_FIELDS = ("artifact_id", "sha256", "size_bytes", "media_type", "compression")
RESULT_SCHEMA = "fs2-serve.nebius.ai/operation-artifact-result/v1"


class EndpointInputError(ValueError):
    """Only field locations and expectations, never rejected private values."""

    code = "MODEL_INPUT_INVALID"

    def __init__(self, model_id: str, issues: list[dict]):
        self.model_id, self.issues = model_id, issues[:12]
        super().__init__("Input does not match the selected endpoint contract")

    def public(self) -> dict:
        return {
            "code": self.code,
            "model_id": self.model_id,
            "issues": self.issues,
            "retryable": False,
            "admission": "not-submitted",
        }


class EndpointArtifactError(ValueError):
    code = "INVALID_OR_UNAUTHORIZED_ARTIFACT"


class NativeResultError(ValueError):
    code = "NATIVE_RESULT_INVALID"

    def __init__(self, message: str, *, operation_id: str | None = None):
        self.operation_id = operation_id
        super().__init__(message)

    def public(self) -> dict:
        return {
            "code": self.code,
            "message": str(self),
            "operation_id": self.operation_id,
            "admission": "accepted" if self.operation_id else "unknown",
            "phase": "result-publication",
            # This is not permission to repeat the completed native invocation.
            "retryable": False,
        }


def _pointer(path) -> str:
    return "/" + "/".join(str(part).replace("~", "~0").replace("/", "~1") for part in path)


def _issue(error: ValidationError) -> dict:
    result = {"field": _pointer(error.absolute_path), "rule": str(error.validator)}
    if error.validator == "required" and isinstance(error.instance, dict):
        result["missing_fields"] = [name for name in error.validator_value if name not in error.instance]
    elif error.validator == "additionalProperties":
        result["allowed_fields"] = sorted(error.schema.get("properties", {}))
    elif error.validator in {
        "type", "enum", "const", "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum",
        "minItems", "maxItems", "minLength", "maxLength", "pattern",
    }:
        result["expected"] = error.validator_value
    return result


def _finite_json(value: Any, path=()) -> list[dict]:
    if isinstance(value, dict):
        if any(not isinstance(key, str) for key in value):
            return [{"field": _pointer(path), "rule": "json_object_keys"}]
        return [issue for key, item in value.items() for issue in _finite_json(item, (*path, key))][:12]
    if isinstance(value, list):
        return [issue for i, item in enumerate(value) for issue in _finite_json(item, (*path, i))][:12]
    if isinstance(value, float) and not math.isfinite(value):
        return [{"field": _pointer(path), "rule": "finite"}]
    if value is not None and not isinstance(value, str | bool | int | float):
        return [{"field": _pointer(path), "rule": "json_value"}]
    return []


def _relationships(model_id: str, value: dict) -> list[dict]:
    """Small runtime DTO checks JSON Schema cannot express; biology stays native."""
    issues = []

    def reject(field, rule):
        issues.append({"field": field, "rule": rule})

    if model_id in {"phenoage", "altumage"}:
        samples = value["samples"]
        if len({item["sample_id"] for item in samples}) != len(samples):
            reject("/samples", "unique_sample_id")
        if model_id == "altumage" and value.get("missing_values", "error") == "error":
            for i, sample in enumerate(samples):
                if isinstance(sample["beta_values"], list) and None in sample["beta_values"]:
                    reject(f"/samples/{i}/beta_values", "null_requires_reference_median")
    if model_id in {"admet-ai", "ctoxpred2"} and isinstance(value.get("molecules"), list):
        molecules = value["molecules"]
        if len({item["id"] for item in molecules}) != len(molecules):
            reject("/molecules", "unique_molecule_id")
    if model_id == "molmim":
        if value.get("num_molecules", 1) > value.get("particles", 2) * value.get("iterations", 1):
            reject("/num_molecules", "particles_times_iterations")
    if model_id == "sam2-1-hiera-large":
        box, points = value.get("box"), value.get("points", [])
        if box and not (box[0] < box[2] and box[1] < box[3]):
            reject("/box", "increasing_coordinates")
        labels = {item.get("object_id", 1) for item in points}
        if box:
            labels.add(value.get("object_id", 1))
        if len(labels) > 8:
            reject("/points", "at_most_eight_objects")
    if model_id == "diffdock" and value.get("steps", 18) > value.get("time_divisions", 20):
        reject("/steps", "at_most_time_divisions")
    return issues


def validate_payload(contract: EndpointContract, payload: dict, *, surface: str = "http") -> dict:
    """No coercion, injected defaults, omitted fields, or opaque payload wrapper."""
    issues = _finite_json(payload)
    if not issues:
        for error in Draft202012Validator(contract.schema(surface)).iter_errors(payload):
            issues.append(_issue(error))
            if len(issues) == 12:
                break
    if not issues:
        issues.extend(_relationships(contract.model_id, payload))
    if issues:
        raise EndpointInputError(contract.model_id, issues)
    return deepcopy(payload)


class EndpointFuncMetadata(FuncMetadata):
    model_id: str
    input_schema: dict[str, Any]
    payload_key: str

    def validate_arguments(self, arguments_to_validate: dict[str, Any]) -> dict[str, Any]:
        issues = _finite_json(arguments_to_validate)
        for error in Draft202012Validator(self.input_schema).iter_errors(arguments_to_validate):
            issues.append(_issue(error))
            if len(issues) >= 12:
                break
        if not issues:
            issues.extend(_relationships(self.model_id, arguments_to_validate))
        if issues:
            error = EndpointInputError(self.model_id, issues)
            raise MCPError(code=INVALID_PARAMS, message=str(error), data=error.public())
        return {self.payload_key: deepcopy(arguments_to_validate)}


def apply_tool_input_contract(server, name: str, contract: EndpointContract, *, payload_key="payload"):
    """The pinned SDK adapter changes advertised fields and executable validation together."""
    tool = server._tool_manager.get_tool(name)
    if tool is None:
        raise ValueError("register the authenticated handler before attaching its contract")
    if contract.tool_name != name:
        raise ValueError("tool name differs from the authoritative App contract")
    schema, previous = contract.schema("mcp"), tool.fn_metadata
    tool.fn_metadata = EndpointFuncMetadata(
        arg_model=previous.arg_model,
        output_schema=previous.output_schema,
        output_model=previous.output_model,
        wrap_output=previous.wrap_output,
        model_id=contract.model_id,
        input_schema=schema,
        payload_key=payload_key,
    )
    tool.parameters = schema


@dataclass(frozen=True)
class _TransportRule:
    path: tuple[str | None, ...]
    encoding: str
    max_bytes: int
    media_types: tuple[str, ...]


def _local_ref(root: dict, reference: str) -> dict:
    if not reference.startswith("#/"):
        raise ValueError("only local endpoint schema references are allowed")
    value = root
    try:
        for name in reference[2:].split("/"):
            value = value[name.replace("~1", "/").replace("~0", "~")]
    except (KeyError, TypeError):
        raise ValueError("endpoint schema reference is unresolved") from None
    return value


def _rules(schema: dict) -> tuple[_TransportRule, ...]:
    rules = {}

    def visit(value, path=(), active=frozenset()):
        if not isinstance(value, dict):
            return
        if _TRANSPORT in value:
            encoding = value[_TRANSPORT]
            limit = value["x-fs2-artifact-max-bytes"]
            media = tuple(value["x-fs2-artifact-media-types"])
            if encoding not in {"json", "utf-8", "base64"} or type(limit) is not int or limit < 1:
                raise ValueError("unsupported endpoint artifact materialization")
            rule = _TransportRule(path, encoding, limit, media)
            if path in rules and rules[path] != rule:
                raise ValueError("conflicting endpoint artifact transports")
            rules[path] = rule
        reference = value.get("$ref")
        if reference and reference not in active:
            visit(_local_ref(schema, reference), path, active | {reference})
        for name, child in value.get("properties", {}).items():
            visit(child, (*path, name), active)
        if isinstance(value.get("items"), dict):
            visit(value["items"], (*path, None), active)
        for keyword in ("allOf", "anyOf", "oneOf"):
            for child in value.get(keyword, []):
                visit(child, path, active)

    visit(schema)
    return tuple(rules.values())


def _matches(value, path):
    if not path:
        return
    head, *tail = path
    keys = range(len(value)) if head is None and isinstance(value, list) else [head]
    for key in keys:
        if not (isinstance(value, dict) and key in value or isinstance(value, list) and isinstance(key, int)):
            continue
        if tail:
            yield from _matches(value[key], tail)
        else:
            yield value, key


def _read_artifact(store: GatewayStore, reference: dict, rule: _TransportRule, *, remaining: int) -> bytes:
    """Resolve metadata only under the authenticated owner's GatewayStore prefix."""
    try:
        if set(reference) != set(_POINTER_FIELDS):
            raise ValueError
        artifact_id = uuid(reference["artifact_id"])
        size = reference["size_bytes"]
        if (
            type(size) is not int or not 0 <= size <= min(rule.max_bytes, remaining)
            or reference["compression"] != "none"
            or reference["media_type"] not in rule.media_types
        ):
            raise ValueError
        record = store.get("artifacts", artifact_id)
        if any(record[name] != reference[name] for name in _POINTER_FIELDS):
            raise ValueError
        key = record["object_key"]
        if not isinstance(key, str) or not key.startswith(store.prefix):
            raise ValueError
        if any(part in {"", ".", ".."} for part in key.split("/")):
            raise ValueError
        # Check declared size and checksum again at use time: uploads can still
        # be replaced through an earlier signed URL until that URL expires.
        response = store.client.get_object(Bucket=store.bucket, Key=key)
        with response["Body"] as source:
            content = source.read(size + 1)
        if len(content) != size or hashlib.sha256(content).hexdigest() != reference["sha256"]:
            raise ValueError
    except (KeyError, ValueError, TypeError, PermissionError):
        raise EndpointArtifactError(
            "Artifact is unavailable or differs from its immutable reference"
        ) from None
    except ClientError as error:
        if error.response["Error"]["Code"] in {"NoSuchKey", "404", "AccessDenied", "403"}:
            raise EndpointArtifactError("Artifact is unavailable to this owner") from None
        raise
    return content


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON fields")
        result[key] = value
    return result


def _inline_size(value, rule: _TransportRule) -> int:
    if value is None:
        return 0
    if rule.encoding == "base64":
        if len(value) > 4 * ((rule.max_bytes + 2) // 3):
            raise EndpointArtifactError("Inline media exceeds the model field byte limit")
        try:
            return len(base64.b64decode(value, validate=True))
        except (ValueError, binascii.Error):
            raise EndpointArtifactError("Inline media is not valid base64") from None
    if rule.encoding == "utf-8":
        return len(value.encode("utf-8"))
    return len(json.dumps(value, separators=(",", ":"), allow_nan=False).encode())


def materialize_inputs(
    contract: EndpointContract,
    payload: dict,
    store: GatewayStore,
    *,
    surface: str = "http",
    max_materialized_bytes: int = 128 * 1024**2,
) -> dict:
    value = validate_payload(contract, payload, surface=surface)
    remaining = max_materialized_bytes
    for rule in _rules(contract.schema()):
        for parent, key in _matches(value, rule.path):
            reference = parent[key]
            if not isinstance(reference, dict) or "artifact_id" not in reference:
                size = _inline_size(reference, rule)
                if size > min(rule.max_bytes, remaining):
                    raise EndpointArtifactError("Inline input exceeds the materialization byte limit")
                remaining -= size
                continue
            content = _read_artifact(store, reference, rule, remaining=remaining)
            remaining -= len(content)
            if rule.encoding == "base64":
                if isinstance(parent, dict) and parent.get("media_type") != reference["media_type"]:
                    raise EndpointArtifactError("Artifact media type differs from the declared model input")
                parent[key] = base64.b64encode(content).decode("ascii")
            else:
                try:
                    decoded = content.decode("utf-8")
                    parent[key] = (
                        json.loads(decoded, object_pairs_hook=_unique_object) if rule.encoding == "json"
                        else decoded
                    )
                except (UnicodeError, ValueError):
                    raise EndpointArtifactError("Artifact is not valid input text or JSON") from None
    # Uploaded JSON must obey the same full-array/nested runtime contract. A
    # valid pointer cannot be used to bypass bounds or introduce new pointers.
    return validate_payload(contract, value, surface="native")


class OutputArtifacts:
    """Bounded binary/large-result publication through the existing artifact store."""

    def __init__(self, store: GatewayStore, *, threshold_bytes=64 * 1024, max_bytes=512 * 1024**2):
        if not 0 < threshold_bytes <= max_bytes:
            raise ValueError("invalid endpoint result size limits")
        self.store, self.threshold_bytes, self.max_bytes = store, threshold_bytes, max_bytes

    def publish(self, operation_id: str, content: bytes, media_type: str) -> dict:
        if len(content) > self.max_bytes:
            raise NativeResultError("Native result exceeds the configured artifact size limit")
        if not isinstance(media_type, str) or "/" not in media_type or any(c in media_type for c in "\r\n"):
            raise NativeResultError("Native result media type is invalid")
        digest = hashlib.sha256(content).hexdigest()
        root = identifier(self.store.owner, "native-result", operation_id)
        aid = identifier(self.store.owner, "native-artifact", canonical([root, digest, media_type]).decode())
        artifacts = self.store.artifacts(root)
        artifacts.objects.put("blobs/" + digest, io.BytesIO(content))
        artifacts.verify("blobs/" + digest, digest, len(content))
        record = {
            "artifact_id": aid,
            "sha256": digest,
            "size_bytes": len(content),
            "media_type": media_type,
            "compression": "none",
            "object_key": self.store.objects(root).prefix + "blobs/" + digest,
        }
        stored = self.store.save("artifacts", aid, record, create=True)
        if any(stored[name] != value for name, value in record.items()):
            raise NativeResultError("Published result artifact identity conflicts")
        return {name: stored[name] for name in _POINTER_FIELDS}

    def _decode(self, value: str) -> bytes:
        if not isinstance(value, str) or len(value) > 4 * ((self.max_bytes + 2) // 3):
            raise NativeResultError("Native binary result exceeds its size bound")
        try:
            content = base64.b64decode(value, validate=True)
        except (ValueError, binascii.Error):
            raise NativeResultError("Native binary result is not valid base64") from None
        if len(content) > self.max_bytes:
            raise NativeResultError("Native binary result exceeds its size bound")
        return content

    def externalize(self, contract: EndpointContract, operation_id: str, result: Any) -> Any:
        if _finite_json(result):
            raise NativeResultError("Native result is not finite JSON")
        value = deepcopy(result)
        if isinstance(value, dict) and value.get("encoding") == "base64":
            if set(value) != {"encoding", "media_type", "size_bytes", "sha256", "data"}:
                raise NativeResultError("Native binary result envelope is invalid")
            content = self._decode(value["data"])
            if (
                type(value["size_bytes"]) is not int
                or value["size_bytes"] != len(content)
                or value["sha256"] != hashlib.sha256(content).hexdigest()
            ):
                raise NativeResultError("Native binary result differs from its declared identity")
            return {
                "schema": RESULT_SCHEMA,
                "artifact": self.publish(operation_id, content, value["media_type"]),
                "content_type": value["media_type"],
            }
        if contract.model_id == "cellpose-cpsam-v2" and isinstance(value, dict):
            for name in ("mask", "overlay"):
                if name + "_base64" in value:
                    content = self._decode(value.pop(name + "_base64"))
                    if value.get(name + "_media_type") != "image/png":
                        raise NativeResultError("Cellpose output media type differs from its contract")
                    value[name + "_artifact"] = self.publish(operation_id, content, "image/png")
        # Never send an unexpected encoded binary field into an agent context.
        def encoded(item):
            if isinstance(item, dict):
                return item.get("encoding") == "base64" or any(
                    key.endswith("_base64") or key == "base64" or encoded(child)
                    for key, child in item.items()
                )
            return isinstance(item, list) and any(encoded(child) for child in item)

        content = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
        if len(content) >= self.threshold_bytes or encoded(value):
            return {
                "schema": RESULT_SCHEMA,
                "artifact": self.publish(operation_id, content, "application/json"),
                "content_type": "application/json",
            }
        return value


def dispatch_native(
    contract: EndpointContract,
    payload: dict,
    *,
    store: GatewayStore,
    invoke: Callable[[str, dict], dict],
    surface: str = "http",
) -> dict:
    """A single call to an already authenticated, exact-model S2 native binding.

    Exceptions propagate to the gateway's structured transport error handling.
    A timeout remains unknown admission; callers must not automatically resubmit.
    This seam has no durable async, cancellation, or idempotency implementation.
    """
    native = materialize_inputs(contract, payload, store, surface=surface)
    response = invoke(contract.model_id, native)
    if not isinstance(response, dict):
        raise NativeResultError("S2 did not supply a native response")
    operation = response.get("operation")
    if isinstance(operation, str):
        operation_id, public_operation = operation, {"id": operation}
    elif isinstance(operation, dict) and isinstance(operation.get("id"), str):
        operation_id = operation["id"]
        if "status" in operation and not isinstance(operation["status"], str):
            raise NativeResultError("S2 operation status is invalid")
        public_operation = {key: operation[key] for key in ("id", "status") if key in operation}
    else:
        raise NativeResultError("S2 did not supply an operation identity")
    if not operation_id or len(operation_id) > 256:
        raise NativeResultError("S2 operation identity is invalid")
    if "result" not in response:
        raise NativeResultError("S2 did not supply a synchronous result", operation_id=operation_id)
    try:
        result = OutputArtifacts(store).externalize(contract, operation_id, response["result"])
    except NativeResultError as error:
        error.operation_id = operation_id
        raise
    except (ClientError, OSError, ValueError):
        raise NativeResultError(
            "Native execution returned but result artifact publication failed", operation_id=operation_id
        ) from None
    return {
        "model_id": contract.model_id,
        "operation": public_operation,
        "result": result,
    }
