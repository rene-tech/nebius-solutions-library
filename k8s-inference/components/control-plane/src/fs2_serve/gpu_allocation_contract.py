"""Bounded contract for node-local GPU allocation observations."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

SCHEMA = "fs2.nebius.ai/gpu-allocation-observations/v2"
DATA_KEY = "observations.json"
MAX_DOCUMENT_BYTES = 256 * 1024
MAX_OBSERVATIONS = 256
MIN_PUBLICATION_TTL_SECONDS = 5
MAX_PUBLICATION_TTL_SECONDS = 300
MAX_PUBLICATION_CLOCK_SKEW_SECONDS = 30

COMPONENT_LABEL = "app.kubernetes.io/component"
COMPONENT_VALUE = "gpu-allocation-observer"
SCHEMA_LABEL = "telemetry.fs2.nebius.ai/publication-schema"
SCHEMA_LABEL_VALUE = "gpu-allocation-v2"
NODE_NAME_ANNOTATION = "telemetry.fs2.nebius.ai/node-name"
POD_NAME_ANNOTATION = "telemetry.fs2.nebius.ai/observer-pod-name"
POD_UID_ANNOTATION = "telemetry.fs2.nebius.ai/observer-pod-uid"
PUBLISHED_AT_ANNOTATION = "telemetry.fs2.nebius.ai/published-at"
EXPIRES_AT_ANNOTATION = "telemetry.fs2.nebius.ai/expires-at"

_GPU_UUID = re.compile(r"^(?:GPU|MIG)-[A-Za-z0-9_.:/-]{1,123}$")
_POD_UID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
_NODE_NAME = re.compile(r"^[a-z0-9](?:[-a-z0-9.]{0,251}[a-z0-9])?$")
_POD_NAME = re.compile(r"^[a-z0-9](?:[-a-z0-9.]{0,251}[a-z0-9])?$")


@dataclass(frozen=True)
class GpuAllocationObservation:
    gpu_uuids: tuple[str, ...]
    observed_at: datetime
    resolution_seconds: float


def allocation_config_map_name(node_name: str) -> str:
    if _NODE_NAME.fullmatch(node_name) is None or node_name == "kube-root-ca.crt":
        raise ValueError("GPU allocation node name is invalid")
    # Kubernetes node names already satisfy the ConfigMap DNS-subdomain name
    # contract. Keeping the object name equal to the node identity lets the
    # admission policy bind it exactly to the projected token's node claim;
    # CEL cannot safely reproduce a source-side hash.
    return node_name


def publication_window(*, published_at: datetime, ttl_seconds: int) -> tuple[str, str]:
    if (
        published_at.tzinfo is None
        or isinstance(ttl_seconds, bool)
        or not MIN_PUBLICATION_TTL_SECONDS <= ttl_seconds <= MAX_PUBLICATION_TTL_SECONDS
    ):
        raise ValueError("GPU allocation publication window is invalid")
    published_at = published_at.astimezone(UTC)
    expires_at = published_at + timedelta(seconds=ttl_seconds)
    return (
        published_at.isoformat().replace("+00:00", "Z"),
        expires_at.isoformat().replace("+00:00", "Z"),
    )


def encode_observations(
    *,
    node_name: str,
    allocations: Mapping[str, tuple[str, ...]],
    observed_at: datetime,
    resolution_seconds: float,
    ttl_seconds: int,
    prior: Mapping[str, GpuAllocationObservation] | None = None,
) -> str:
    allocation_config_map_name(node_name)
    published_at, expires_at = publication_window(
        published_at=observed_at,
        ttl_seconds=ttl_seconds,
    )
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
            "published_at": published_at,
            "expires_at": expires_at,
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
    observed_at: datetime | None = None,
) -> dict[str, GpuAllocationObservation]:
    expected_name = allocation_config_map_name(expected_node_name)
    metadata = config_map.get("metadata")
    if not isinstance(metadata, Mapping) or metadata.get("name") != expected_name:
        raise ValueError("GPU allocation publication name is invalid")
    labels = metadata.get("labels")
    annotations = metadata.get("annotations")
    owner_references = metadata.get("ownerReferences")
    if (
        not isinstance(labels, Mapping)
        or labels.get(COMPONENT_LABEL) != COMPONENT_VALUE
        or labels.get(SCHEMA_LABEL) != SCHEMA_LABEL_VALUE
        or not isinstance(annotations, Mapping)
        or annotations.get(NODE_NAME_ANNOTATION) != expected_node_name
        or not isinstance(annotations.get(POD_NAME_ANNOTATION), str)
        or _POD_NAME.fullmatch(annotations[POD_NAME_ANNOTATION]) is None
        or not isinstance(annotations.get(POD_UID_ANNOTATION), str)
        or _POD_UID.fullmatch(annotations[POD_UID_ANNOTATION]) is None
        or not isinstance(owner_references, list)
        or len(owner_references) != 1
    ):
        raise ValueError("GPU allocation publication custody is invalid")
    owner = owner_references[0]
    if (
        not isinstance(owner, Mapping)
        or set(owner) != {"apiVersion", "kind", "name", "uid", "controller", "blockOwnerDeletion"}
        or owner.get("apiVersion") != "v1"
        or owner.get("kind") != "Pod"
        or owner.get("name") != annotations[POD_NAME_ANNOTATION]
        or owner.get("uid") != annotations[POD_UID_ANNOTATION]
        or owner.get("controller") is not False
        or owner.get("blockOwnerDeletion") is not False
    ):
        raise ValueError("GPU allocation publication owner is invalid")
    data = config_map.get("data")
    raw = data.get(DATA_KEY) if isinstance(data, Mapping) else None
    if not isinstance(raw, str) or not 1 <= len(raw.encode()) <= MAX_DOCUMENT_BYTES:
        raise ValueError("GPU allocation observation document is absent or too large")
    try:
        document = json.loads(raw)
    except (json.JSONDecodeError, RecursionError):
        raise ValueError("GPU allocation observation document is invalid JSON") from None
    if not isinstance(document, Mapping) or set(document) != {
        "schema",
        "node_name",
        "published_at",
        "expires_at",
        "pods",
    }:
        raise ValueError("GPU allocation observation document shape is invalid")
    if document["schema"] != SCHEMA or document["node_name"] != expected_node_name:
        raise ValueError("GPU allocation observation authority is invalid")
    try:
        raw_published_at = document["published_at"]
        raw_expires_at = document["expires_at"]
        if not isinstance(raw_published_at, str) or not isinstance(raw_expires_at, str):
            raise ValueError
        published_at = datetime.fromisoformat(raw_published_at.replace("Z", "+00:00"))
        expires_at = datetime.fromisoformat(raw_expires_at.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        raise ValueError("GPU allocation publication time is invalid") from None
    if published_at.tzinfo is None or expires_at.tzinfo is None:
        raise ValueError("GPU allocation publication lifetime is invalid")
    ttl_seconds = (expires_at - published_at).total_seconds()
    if (
        not MIN_PUBLICATION_TTL_SECONDS <= ttl_seconds <= MAX_PUBLICATION_TTL_SECONDS
        or annotations.get(PUBLISHED_AT_ANNOTATION) != raw_published_at
        or annotations.get(EXPIRES_AT_ANNOTATION) != raw_expires_at
    ):
        raise ValueError("GPU allocation publication lifetime is invalid")
    if observed_at is not None:
        if observed_at.tzinfo is None:
            raise ValueError("GPU allocation observation time is invalid")
        observed_at = observed_at.astimezone(UTC)
        if (
            published_at.astimezone(UTC)
            > observed_at + timedelta(seconds=MAX_PUBLICATION_CLOCK_SKEW_SECONDS)
            or expires_at.astimezone(UTC) <= observed_at
        ):
            raise ValueError("GPU allocation publication is stale or future-dated")
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
