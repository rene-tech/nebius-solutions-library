"""Bounded contract for node-local GPU allocation observations."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

SCHEMA = "fs2.nebius.ai/gpu-allocation-observations/v1"
DATA_KEY = "observations.json"
MAX_DOCUMENT_BYTES = 256 * 1024
MAX_OBSERVATIONS = 256

_GPU_UUID = re.compile(r"^(?:GPU|MIG)-[A-Za-z0-9_.:/-]{1,123}$")
_POD_UID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
_NODE_NAME = re.compile(r"^[a-z0-9](?:[-a-z0-9.]{0,251}[a-z0-9])?$")


@dataclass(frozen=True)
class GpuAllocationObservation:
    gpu_uuids: tuple[str, ...]
    observed_at: datetime
    resolution_seconds: float


def allocation_config_map_name(node_name: str) -> str:
    if _NODE_NAME.fullmatch(node_name) is None:
        raise ValueError("GPU allocation node name is invalid")
    digest = hashlib.sha256(node_name.encode()).hexdigest()[:32]
    return f"fs2-gpu-allocations-{digest}"


def encode_observations(
    *,
    node_name: str,
    allocations: Mapping[str, tuple[str, ...]],
    observed_at: datetime,
    resolution_seconds: float,
    prior: Mapping[str, GpuAllocationObservation] | None = None,
) -> str:
    allocation_config_map_name(node_name)
    if (
        len(allocations) > MAX_OBSERVATIONS
        or isinstance(resolution_seconds, bool)
        or not 0.1 <= resolution_seconds <= 30
        or observed_at.tzinfo is None
    ):
        raise ValueError("GPU allocation observation is outside its bound")
    pods: dict[str, dict[str, object]] = {}
    for pod_uid, gpu_uuids in sorted(allocations.items()):
        if (
            _POD_UID.fullmatch(pod_uid) is None
            or not 1 <= len(gpu_uuids) <= 64
            or len(set(gpu_uuids)) != len(gpu_uuids)
            or any(_GPU_UUID.fullmatch(value) is None for value in gpu_uuids)
        ):
            raise ValueError("GPU allocation observation contains an invalid identity")
        previous = (prior or {}).get(pod_uid)
        first_observed_at = (
            previous.observed_at
            if previous is not None and previous.gpu_uuids == gpu_uuids
            else observed_at.astimezone(UTC)
        )
        pods[pod_uid] = {
            "gpu_uuids": list(gpu_uuids),
            "observed_at": first_observed_at.isoformat().replace("+00:00", "Z"),
            "resolution_seconds": resolution_seconds,
        }
    value = json.dumps(
        {
            "schema": SCHEMA,
            "node_name": node_name,
            "pods": pods,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    if len(value.encode()) > MAX_DOCUMENT_BYTES:
        raise ValueError("GPU allocation observation document is too large")
    return value


def parse_observations(
    config_map: Mapping[str, Any],
    *,
    expected_node_name: str,
) -> dict[str, GpuAllocationObservation]:
    allocation_config_map_name(expected_node_name)
    data = config_map.get("data")
    raw = data.get(DATA_KEY) if isinstance(data, Mapping) else None
    if not isinstance(raw, str) or not 1 <= len(raw.encode()) <= MAX_DOCUMENT_BYTES:
        raise ValueError("GPU allocation observation document is absent or too large")
    try:
        document = json.loads(raw)
    except (json.JSONDecodeError, RecursionError):
        raise ValueError("GPU allocation observation document is invalid JSON") from None
    if not isinstance(document, Mapping) or set(document) != {"schema", "node_name", "pods"}:
        raise ValueError("GPU allocation observation document shape is invalid")
    if document["schema"] != SCHEMA or document["node_name"] != expected_node_name:
        raise ValueError("GPU allocation observation authority is invalid")
    pods = document["pods"]
    if (
        not isinstance(pods, Mapping)
        or len(pods) > MAX_OBSERVATIONS
    ):
        raise ValueError("GPU allocation observation is outside its bound")
    result: dict[str, GpuAllocationObservation] = {}
    for pod_uid, raw_observation in pods.items():
        if not isinstance(raw_observation, Mapping) or set(raw_observation) != {
            "gpu_uuids",
            "observed_at",
            "resolution_seconds",
        }:
            raise ValueError("GPU allocation observation shape is invalid")
        raw_uuids = raw_observation["gpu_uuids"]
        if (
            not isinstance(pod_uid, str)
            or _POD_UID.fullmatch(pod_uid) is None
            or not isinstance(raw_uuids, list)
            or not 1 <= len(raw_uuids) <= 64
            or not all(isinstance(value, str) and _GPU_UUID.fullmatch(value) is not None for value in raw_uuids)
            or len(set(raw_uuids)) != len(raw_uuids)
        ):
            raise ValueError("GPU allocation observation contains an invalid identity")
        try:
            raw_observed_at = raw_observation["observed_at"]
            raw_resolution = raw_observation["resolution_seconds"]
            if not isinstance(raw_observed_at, str) or isinstance(raw_resolution, bool):
                raise ValueError
            observed_at = datetime.fromisoformat(raw_observed_at.replace("Z", "+00:00"))
            resolution = float(raw_resolution)
        except (TypeError, ValueError):
            raise ValueError("GPU allocation observation timing is invalid") from None
        if observed_at.tzinfo is None or not 0.1 <= resolution <= 30:
            raise ValueError("GPU allocation observation timing is outside its bound")
        result[pod_uid] = GpuAllocationObservation(
            gpu_uuids=tuple(raw_uuids),
            observed_at=observed_at.astimezone(UTC),
            resolution_seconds=resolution,
        )
    return result
