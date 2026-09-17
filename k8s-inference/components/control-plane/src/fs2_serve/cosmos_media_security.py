"""Fail-closed admission policy for Cosmos media references.

Public callers identify media through finalized, tenant-owned artifact metadata.
Only the worker may resolve and materialize those bytes at the runtime boundary;
the GPU runtime must never receive a caller-selected network location.
"""

# ruff: noqa: F811 -- the rejected v1 policy remains as immutable review evidence;
# the additive v2 definition below is the exported implementation.

from __future__ import annotations

import json
from typing import Any, Final
from uuid import UUID

from .model_input_contracts import (
    COSMOS_CONTROL_MAX_BYTES,
    COSMOS_IMAGE_MEDIA_TYPES,
    COSMOS_PRIMARY_MAX_BYTES,
    COSMOS_REQUEST_MAX_BYTES,
    COSMOS_VIDEO_MEDIA_TYPES,
)
from .registry import OperationalModel
from .scientific_run_result import ArtifactRef, Compression

_MEDIA_TYPES: Final = frozenset(
    {"image/jpeg", "image/png", "image/webp", "video/mp4", "application/mp4"}
)
_INPUT_MAX_BYTES: Final = 512 * 1024 * 1024
_CONTROL_MAX_BYTES: Final = 128 * 1024 * 1024


class CosmosMediaReferenceError(ValueError):
    """A Cosmos request tried to bypass tenant artifact materialization."""

    code = "cosmos_media_reference_invalid"


def _artifact_reference(value: Any, *, field: str, max_bytes: int) -> None:
    try:
        reference = ArtifactRef.model_validate(value)
        UUID(reference.artifact_id)
    except (TypeError, ValueError) as error:
        raise CosmosMediaReferenceError(f"{field} must be a finalized platform artifact reference") from error
    if reference.compression is not Compression.NONE:
        raise CosmosMediaReferenceError(f"{field} must reference uncompressed artifact bytes")
    if reference.media_type not in _MEDIA_TYPES or reference.size_bytes > max_bytes:
        raise CosmosMediaReferenceError(f"{field} artifact metadata is outside the Cosmos media contract")


def enforce_cosmos_media_reference_policy(
    model: OperationalModel,
    protocol: str,
    request_body: bytes,
) -> None:
    """Reject URL/path media locators before durable operation admission."""

    model_ref = model.dynamic_policy.publication.source_model_ref if model.dynamic_policy else model.id
    if model_ref != "cosmos3-nano" or protocol != "native":
        return
    try:
        payload = json.loads(request_body)
    except (RecursionError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise CosmosMediaReferenceError("Cosmos media input must be a JSON object") from error
    if not isinstance(payload, dict):
        raise CosmosMediaReferenceError("Cosmos media input must be a JSON object")

    for name in ("input_reference", "vision_path"):
        if name in payload:
            _artifact_reference(payload[name], field=name, max_bytes=_INPUT_MAX_BYTES)

    controls = payload.get("controls")
    if controls is None:
        return
    if not isinstance(controls, list):
        raise CosmosMediaReferenceError("controls must be an array of typed control objects")
    for index, control in enumerate(controls):
        if not isinstance(control, dict):
            raise CosmosMediaReferenceError("controls must contain only typed control objects")
        if "reference" in control:
            _artifact_reference(
                control["reference"],
                field=f"controls[{index}].reference",
                max_bytes=_CONTROL_MAX_BYTES,
            )


__all__ = ["CosmosMediaReferenceError", "enforce_cosmos_media_reference_policy"]


def _cosmos_model(model: OperationalModel, protocol: str) -> bool:
    model_ref = model.dynamic_policy.publication.source_model_ref if model.dynamic_policy else model.id
    return model_ref == "cosmos3-nano" and protocol == "native"


def _cosmos_payload(request_body: bytes) -> dict[str, Any]:
    try:
        payload = json.loads(request_body)
    except (RecursionError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise CosmosMediaReferenceError("Cosmos media input must be a JSON object") from error
    if not isinstance(payload, dict):
        raise CosmosMediaReferenceError("Cosmos media input must be a JSON object")
    return payload


def _primary_media_types(mode: Any) -> frozenset[str]:
    if mode == "transfer-video":
        return frozenset(COSMOS_VIDEO_MEDIA_TYPES)
    if mode == "image-to-video":
        return frozenset(COSMOS_IMAGE_MEDIA_TYPES)
    if mode == "video-to-video":
        return frozenset(COSMOS_VIDEO_MEDIA_TYPES)
    return frozenset(COSMOS_IMAGE_MEDIA_TYPES + COSMOS_VIDEO_MEDIA_TYPES)


def _strict_artifact_reference(
    value: Any,
    *,
    field: str,
    max_bytes: int,
    media_types: frozenset[str],
) -> ArtifactRef:
    try:
        reference = ArtifactRef.model_validate(value)
        UUID(reference.artifact_id)
    except (TypeError, ValueError) as error:
        raise CosmosMediaReferenceError(
            f"{field} must be a finalized platform artifact reference"
        ) from error
    if reference.compression is not Compression.NONE:
        raise CosmosMediaReferenceError(f"{field} must reference uncompressed artifact bytes")
    if reference.media_type not in media_types or reference.size_bytes > max_bytes:
        raise CosmosMediaReferenceError(
            f"{field} artifact metadata is outside the Cosmos media contract"
        )
    return reference


def _artifact_references(payload: dict[str, Any]) -> tuple[ArtifactRef, ...]:
    mode = payload.get("mode")
    direct = [name for name in ("input_reference", "vision_path") if name in payload]
    if len(direct) > 1:
        raise CosmosMediaReferenceError("provide input_reference or vision_path, not both")
    if mode in {"text-to-image", "text-to-video"} and direct:
        raise CosmosMediaReferenceError("the selected Cosmos mode does not accept source media")
    if mode in {"image-to-video", "video-to-video"} and not direct:
        raise CosmosMediaReferenceError("the selected Cosmos mode requires a platform artifact")

    references: list[ArtifactRef] = []
    if direct:
        references.append(
            _strict_artifact_reference(
                payload[direct[0]],
                field=direct[0],
                max_bytes=COSMOS_PRIMARY_MAX_BYTES,
                media_types=_primary_media_types(mode),
            )
        )

    controls = payload.get("controls")
    if controls is not None:
        if not isinstance(controls, list) or not 1 <= len(controls) <= 5:
            raise CosmosMediaReferenceError("controls must be a bounded array of typed control objects")
        for index, control in enumerate(controls):
            if not isinstance(control, dict):
                raise CosmosMediaReferenceError("controls must contain only typed control objects")
            if "reference" in control:
                references.append(
                    _strict_artifact_reference(
                        control["reference"],
                        field=f"controls[{index}].reference",
                        max_bytes=COSMOS_CONTROL_MAX_BYTES,
                        media_types=frozenset(COSMOS_IMAGE_MEDIA_TYPES + COSMOS_VIDEO_MEDIA_TYPES),
                    )
                )
    if sum(reference.size_bytes for reference in references) > COSMOS_REQUEST_MAX_BYTES:
        raise CosmosMediaReferenceError("Cosmos artifact inputs exceed the per-request byte budget")
    return tuple(references)


def enforce_cosmos_media_reference_policy(
    model: OperationalModel,
    protocol: str,
    request_body: bytes,
) -> None:
    """Require bounded artifact metadata before a Cosmos row is admitted."""

    if not _cosmos_model(model, protocol):
        return
    _artifact_references(_cosmos_payload(request_body))


def enforce_cosmos_dispatch_policy(
    model: OperationalModel,
    protocol: str,
    request_body: bytes,
) -> None:
    """Revalidate retained rows before any materialization or runtime call."""

    enforce_cosmos_media_reference_policy(model, protocol, request_body)


def _materialized_reference(
    value: Any,
    *,
    field: str,
    max_bytes: int,
    media_types: frozenset[str],
) -> int:
    if not isinstance(value, str):
        raise CosmosMediaReferenceError(f"{field} was not materialized for runtime dispatch")
    metadata, separator, encoded = value.partition(",")
    if separator != "," or not metadata.startswith("data:") or not metadata.lower().endswith(";base64"):
        raise CosmosMediaReferenceError(f"{field} is not a platform-materialized data URL")
    media_type = metadata[5:-7].lower()
    if media_type not in media_types:
        raise CosmosMediaReferenceError(f"{field} media type is outside the Cosmos runtime contract")
    base64_alphabet = frozenset(
        "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/="
    )
    padding = 2 if encoded.endswith("==") else 1 if encoded.endswith("=") else 0
    first_padding = encoded.find("=")
    if (
        not encoded
        or len(encoded) % 4
        or any(character not in base64_alphabet for character in encoded)
        or (first_padding != -1 and first_padding != len(encoded) - padding)
    ):
        raise CosmosMediaReferenceError(f"{field} is not canonical base64")
    decoded_bytes = (len(encoded) // 4) * 3 - padding
    if decoded_bytes > max_bytes:
        raise CosmosMediaReferenceError(f"{field} exceeds the Cosmos runtime byte budget")
    return decoded_bytes
    if len(encoded) > 4 * (max_bytes // 3 + 1) + 4:
        raise CosmosMediaReferenceError(f"{field} exceeds the Cosmos runtime byte budget")
    return (len(encoded) * 3) // 4


def enforce_cosmos_runtime_payload_policy(
    model: OperationalModel,
    protocol: str,
    request_body: bytes,
) -> None:
    """Allow only bounded platform-materialized media at the runtime boundary."""

    if not _cosmos_model(model, protocol):
        return
    payload = _cosmos_payload(request_body)
    mode = payload.get("mode")
    total = 0
    for name in ("input_reference", "vision_path"):
        if name in payload:
            total += _materialized_reference(
                payload[name],
                field=name,
                max_bytes=COSMOS_PRIMARY_MAX_BYTES,
                media_types=_primary_media_types(mode),
            )
    controls = payload.get("controls")
    if controls is not None:
        if not isinstance(controls, list) or not 1 <= len(controls) <= 5:
            raise CosmosMediaReferenceError("controls must be a bounded array of typed control objects")
        for index, control in enumerate(controls):
            if not isinstance(control, dict):
                raise CosmosMediaReferenceError("controls must contain only typed control objects")
            if "reference" in control:
                total += _materialized_reference(
                    control["reference"],
                    field=f"controls[{index}].reference",
                    max_bytes=COSMOS_CONTROL_MAX_BYTES,
                    media_types=frozenset(COSMOS_IMAGE_MEDIA_TYPES + COSMOS_VIDEO_MEDIA_TYPES),
                )
    if total > COSMOS_REQUEST_MAX_BYTES:
        raise CosmosMediaReferenceError("Cosmos materialized inputs exceed the per-request byte budget")


__all__ = [
    "CosmosMediaReferenceError",
    "enforce_cosmos_dispatch_policy",
    "enforce_cosmos_media_reference_policy",
    "enforce_cosmos_runtime_payload_policy",
]
