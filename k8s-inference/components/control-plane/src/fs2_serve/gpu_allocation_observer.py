"""Node-local publication of kubelet NVIDIA allocation identity.

The kubelet device-plugin checkpoint is the local authority that binds a Pod
UID to physical GPU UUIDs.  This observer publishes only that bounded mapping
and the instant at which it first saw the allocation.  It does not infer an
allocation start time and never overwrites a conflicting Pod annotation.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

from .model_deployment import MODEL_ID_LABEL
from .runtime_kubernetes import (
    GPU_ALLOCATION_OBSERVED_AT_ANNOTATION,
    GPU_OBSERVER_RESOLUTION_ANNOTATION,
    GPU_UUIDS_ANNOTATION,
    pod_gpu_count,
)

LOGGER = logging.getLogger(__name__)

MAX_CHECKPOINT_BYTES = 4 * 1024 * 1024
MAX_POD_LIST_BYTES = 4 * 1024 * 1024
MAX_PODS = 4096
SCIENTIFIC_MODEL_ID_LABEL = "fs2.nebius.ai/model-id"
_GPU_UUID = re.compile(r"^(?:GPU|MIG)-[A-Za-z0-9_.:/-]{1,123}$")
_POD_UID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")


def _mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _sequence(value: object) -> Sequence[Any]:
    return value if isinstance(value, list) else ()


def parse_kubelet_device_checkpoint(payload: bytes) -> dict[str, tuple[str, ...]]:
    """Return exact Pod UID -> sorted GPU UUID mappings from a bounded checkpoint."""

    if not payload or len(payload) > MAX_CHECKPOINT_BYTES:
        raise ValueError("kubelet device checkpoint size is invalid")
    try:
        document = json.loads(payload)
    except (json.JSONDecodeError, UnicodeDecodeError, RecursionError):
        raise ValueError("kubelet device checkpoint is invalid JSON") from None
    data_value = _mapping(document).get("Data")
    if isinstance(data_value, str):
        try:
            data_value = json.loads(data_value)
        except (json.JSONDecodeError, RecursionError):
            raise ValueError("kubelet device checkpoint Data is invalid") from None
    entries = _sequence(_mapping(data_value).get("PodDeviceEntries"))
    allocations: dict[str, set[str]] = {}
    for raw_entry in entries:
        entry = _mapping(raw_entry)
        pod_uid = entry.get("PodUID")
        resource_name = entry.get("ResourceName")
        if (
            not isinstance(pod_uid, str)
            or _POD_UID.fullmatch(pod_uid) is None
            or not isinstance(resource_name, str)
            or not (resource_name == "nvidia.com/gpu" or resource_name.startswith("nvidia.com/mig-"))
        ):
            continue
        device_ids = _mapping(entry.get("DeviceIDs"))
        discovered: set[str] = set()
        for raw_ids in device_ids.values():
            for raw_id in _sequence(raw_ids):
                if isinstance(raw_id, str) and _GPU_UUID.fullmatch(raw_id) is not None:
                    discovered.add(raw_id)
        if discovered:
            allocations.setdefault(pod_uid, set()).update(discovered)
    return {pod_uid: tuple(sorted(values)) for pod_uid, values in sorted(allocations.items())}


def read_kubelet_device_checkpoint(path: Path) -> dict[str, tuple[str, ...]]:
    try:
        size = path.stat().st_size
        if not 1 <= size <= MAX_CHECKPOINT_BYTES:
            raise ValueError("kubelet device checkpoint size is invalid")
        payload = path.read_bytes()
    except OSError as exc:
        raise ValueError("kubelet device checkpoint is unavailable") from exc
    return parse_kubelet_device_checkpoint(payload)


def _has_unambiguous_model_label(labels: Mapping[str, Any]) -> bool:
    """Accept serving and scientific Pods, but reject conflicting identities."""

    serving_model_id = labels.get(MODEL_ID_LABEL)
    scientific_model_id = labels.get(SCIENTIFIC_MODEL_ID_LABEL)
    values = [value for value in (serving_model_id, scientific_model_id) if isinstance(value, str) and value]
    return bool(values) and len(set(values)) == 1


@dataclass(frozen=True)
class KubernetesGpuAllocationPublisher:
    base_url: str
    token_file: Path
    ca_file: Path
    namespace: str
    node_name: str
    poll_seconds: float

    def __post_init__(self) -> None:
        parsed = urlsplit(self.base_url)
        if (
            parsed.scheme != "https"
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or parsed.path not in {"", "/"}
        ):
            raise ValueError("GPU observer Kubernetes API URL must be credential-free HTTPS")

    def _headers(self) -> dict[str, str]:
        try:
            token = self.token_file.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise RuntimeError("GPU observer service-account token is unavailable") from exc
        if not 32 <= len(token) <= 16 * 1024:
            raise RuntimeError("GPU observer service-account token is invalid")
        return {"authorization": f"Bearer {token}", "accept": "application/json"}

    async def publish_once(
        self,
        client: httpx.AsyncClient,
        allocations: Mapping[str, tuple[str, ...]],
        *,
        observed_at: datetime,
    ) -> int:
        # Projected service-account tokens rotate; reload before every bounded
        # poll instead of pinning the bootstrap token for the process lifetime.
        client.headers.update(self._headers())
        response = await client.get(
            f"/api/v1/namespaces/{self.namespace}/pods",
            params={"fieldSelector": f"spec.nodeName={self.node_name}", "limit": str(MAX_PODS)},
        )
        if response.status_code != 200 or len(response.content) > MAX_POD_LIST_BYTES:
            raise RuntimeError("GPU observer Pod list failed")
        try:
            document = response.json()
        except (json.JSONDecodeError, UnicodeDecodeError, RecursionError):
            raise RuntimeError("GPU observer Pod list is invalid") from None
        pods = _sequence(_mapping(document).get("items"))
        if len(pods) > MAX_PODS:
            raise RuntimeError("GPU observer Pod list exceeded its bound")

        published = 0
        for raw_pod in pods:
            pod = _mapping(raw_pod)
            metadata = _mapping(pod.get("metadata"))
            labels = _mapping(metadata.get("labels"))
            pod_uid = metadata.get("uid")
            pod_name = metadata.get("name")
            resource_version = metadata.get("resourceVersion")
            if (
                not _has_unambiguous_model_label(labels)
                or not isinstance(pod_uid, str)
                or not isinstance(pod_name, str)
                or not isinstance(resource_version, str)
            ):
                continue
            gpu_uuids = allocations.get(pod_uid)
            gpu_count = pod_gpu_count(pod)
            if gpu_uuids is None or gpu_count is None or len(gpu_uuids) != gpu_count:
                continue
            annotations = _mapping(metadata.get("annotations"))
            expected_gpu_json = json.dumps(gpu_uuids, separators=(",", ":"))
            existing = {
                GPU_UUIDS_ANNOTATION: annotations.get(GPU_UUIDS_ANNOTATION),
                GPU_ALLOCATION_OBSERVED_AT_ANNOTATION: annotations.get(GPU_ALLOCATION_OBSERVED_AT_ANNOTATION),
                GPU_OBSERVER_RESOLUTION_ANNOTATION: annotations.get(GPU_OBSERVER_RESOLUTION_ANNOTATION),
            }
            if all(value is not None for value in existing.values()):
                # An identical existing mapping remains the first observation;
                # a conflict is never overwritten by a later poll.
                continue
            if any(value is not None for value in existing.values()):
                continue
            body = {
                "metadata": {
                    "resourceVersion": resource_version,
                    "annotations": {
                        GPU_UUIDS_ANNOTATION: expected_gpu_json,
                        GPU_ALLOCATION_OBSERVED_AT_ANNOTATION: observed_at.astimezone(UTC)
                        .isoformat()
                        .replace("+00:00", "Z"),
                        GPU_OBSERVER_RESOLUTION_ANNOTATION: str(self.poll_seconds),
                    },
                }
            }
            patched = await client.patch(
                f"/api/v1/namespaces/{self.namespace}/pods/{pod_name}",
                headers={"content-type": "application/merge-patch+json"},
                content=json.dumps(body, separators=(",", ":")).encode(),
            )
            if patched.status_code in {200, 201}:
                published += 1
            elif patched.status_code != 409:
                raise RuntimeError("GPU observer Pod annotation failed")
        return published


async def run_gpu_allocation_observer(
    *,
    publisher: KubernetesGpuAllocationPublisher,
    checkpoint_file: Path,
) -> None:
    timeout = httpx.Timeout(max(2.0, publisher.poll_seconds * 2))
    async with httpx.AsyncClient(
        base_url=publisher.base_url,
        verify=str(publisher.ca_file),
        timeout=timeout,
        trust_env=False,
    ) as client:
        while True:
            try:
                allocations = read_kubelet_device_checkpoint(checkpoint_file)
                await publisher.publish_once(client, allocations, observed_at=datetime.now(UTC))
            except asyncio.CancelledError:
                raise
            except (httpx.HTTPError, RuntimeError, ValueError):
                # The observer is retried locally; model serving is never gated
                # on telemetry publication and no exception body is logged.
                LOGGER.warning("GPU allocation observation failed")
            await asyncio.sleep(publisher.poll_seconds)
