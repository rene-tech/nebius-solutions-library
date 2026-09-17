"""Fail-closed admission policy for Cosmos media references.

Public callers identify media through finalized, tenant-owned artifact metadata.
Only the worker may resolve and materialize those bytes at the runtime boundary;
the GPU runtime must never receive a caller-selected network location.
"""

from __future__ import annotations

import json
from typing import Any, Final
from uuid import UUID

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
