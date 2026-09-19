"""Bounded static LeRobot worker failures; never publish termination text."""

from __future__ import annotations

import json
from collections.abc import Mapping
from types import MappingProxyType

LEROBOT_MODEL_ID = "cosmos3-lerobot-augmentation"
TERMINATION_SCHEMA = "fs2-serve.nebius.ai/lerobot-worker-error/v1"
TERMINATION_MAX_BYTES = 1024
ERROR_DETAILS: Mapping[str, str] = MappingProxyType(
    {
        "INVALID_REQUEST": "The augmentation request is invalid; check the published request schema.",
        "CANCELLED": "The augmentation was cancelled before completion; no successful dataset is claimed.",
        "DATASET_INVALID": (
            "The dataset or generated media failed validation; check dataset format, fixed-rate timing, "
            "selection and workspace limits."
        ),
        "PLATFORM_RESPONSE_INVALID": (
            "Control-plane operation or artifact response did not match the supported contract; "
            "contact the operator with the parent operation ID."
        ),
        "PLATFORM_UPSTREAM_ERROR": "The control plane rejected or could not process a delegated Cosmos request.",
        "REFERENCE_TOO_LARGE": "The selected episode reference video exceeds the supported upload bound.",
        "RUNTIME_DEPENDENCY_MISSING": "The worker is missing a required runtime dependency; contact the operator.",
        "COSMOS_MEDIA_UNREADABLE": "A Cosmos video could not be read.",
        "COSMOS_MEDIA_INVALID": "A Cosmos video is invalid or exceeds its supported size bound.",
        "COSMOS_MEDIA_ALIGNMENT_INVALID": (
            "Generated video dimensions, frame count or FPS differ from the source episode; "
            "no resizing or retiming was applied."
        ),
        "COSMOS_MEDIA_NORMALIZATION_FAILED": (
            "The generated video could not be restored to the source dataset geometry."
        ),
        "COSMOS_OPERATION_FAILED": "A delegated Cosmos operation failed; inspect its child operation status.",
        "COSMOS_OPERATION_TIMEOUT": "The delegated Cosmos operation exceeded the worker wait timeout.",
        "COSMOS_OUTPUT_IDENTITY_MISMATCH": "The downloaded Cosmos artifact did not match its declared digest or size.",
        "COSMOS_OUTPUT_TOO_LARGE": "The generated Cosmos artifact exceeds the supported output bound.",
        "COSMOS_TRANSPORT_ERROR": "Communication with the control plane or artifact download failed.",
    }
)
RETRYABLE_CODES = frozenset({"PLATFORM_UPSTREAM_ERROR", "COSMOS_OPERATION_TIMEOUT", "COSMOS_TRANSPORT_ERROR"})
# A controller can observe an already-running worker from the prior image during
# a rollout. Accept that one published static wording as well; public projection
# still uses ERROR_DETAILS, never arbitrary received termination text.
LEGACY_ERROR_DETAILS: Mapping[str, frozenset[str]] = MappingProxyType(
    {"COSMOS_MEDIA_ALIGNMENT_INVALID": frozenset({"The generated video frame count differs from the source episode."})}
)


def _unique_fields(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value = dict(pairs)
    if len(value) != len(pairs):
        raise ValueError("duplicate termination fields")
    return value


def worker_error_code(message: object) -> str | None:
    """Accept only this exact immutable report, without retaining received text."""
    if not isinstance(message, str):
        return None
    try:
        if len(message.encode("utf-8")) > TERMINATION_MAX_BYTES:
            return None
        value = json.loads(message, object_pairs_hook=_unique_fields)
    except (ValueError, UnicodeError, RecursionError):
        return None
    if not isinstance(value, dict) or set(value) != {"schema", "code", "detail", "retryable"}:
        return None
    code = value["code"]
    detail = value["detail"]
    retryable = value["retryable"]
    if (
        value["schema"] != TERMINATION_SCHEMA
        or not isinstance(code, str)
        or code not in ERROR_DETAILS
        or not isinstance(detail, str)
        or len(detail) > 256
        or (detail != ERROR_DETAILS[code] and detail not in LEGACY_ERROR_DETAILS.get(code, frozenset()))
        or type(retryable) is not bool
        or (retryable and code not in RETRYABLE_CODES)
    ):
        return None
    return code


def worker_error_detail(model_id: str, code: str | None) -> str | None:
    """Only the LeRobot model may project these operator-owned public strings."""
    return ERROR_DETAILS.get(code) if model_id == LEROBOT_MODEL_ID and code is not None else None
