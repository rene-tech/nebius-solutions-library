"""Node-local publication of kubelet NVIDIA allocation identity.

The kubelet device-plugin checkpoint is the local authority that binds a Pod
UID to physical GPU UUIDs.  This observer publishes only that bounded mapping
and the instant at which it saw the allocation into its isolated namespace.
It does not infer an allocation start time and never mutates workload Pods.
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

from .gpu_allocation_contract import (
    DATA_KEY,
    allocation_config_map_name,
    encode_observations,
    parse_observations,
)
from .model_deployment import MODEL_ID_LABEL
from .runtime_kubernetes import pod_gpu_count

LOGGER = logging.getLogger(__name__)

MAX_CHECKPOINT_BYTES = 4 * 1024 * 1024
MAX_POD_LIST_BYTES = 4 * 1024 * 1024
MAX_PODS = 4096
MAX_NAMESPACES = 32
SCIENTIFIC_MODEL_ID_LABEL = "fs2.nebius.ai/model-id"
_GPU_UUID = re.compile(r"^(?:GPU|MIG)-[A-Za-z0-9_.:/-]{1,123}$")
_POD_UID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
_NAMESPACE = re.compile(r"^[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?$")


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
    namespaces: tuple[str, ...]
    publication_namespace: str
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
        if (
            not self.namespaces
            or len(self.namespaces) > MAX_NAMESPACES
            or len(set(self.namespaces)) != len(self.namespaces)
            or any(_NAMESPACE.fullmatch(namespace) is None for namespace in self.namespaces)
            or _NAMESPACE.fullmatch(self.publication_namespace) is None
        ):
            raise ValueError("GPU observer namespaces are invalid")

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
        observed: dict[str, tuple[str, ...]] = {}
        for namespace in self.namespaces:
            namespace_observations = await self._observe_namespace(
                client,
                allocations,
                namespace=namespace,
            )
            for pod_uid, gpu_uuids in namespace_observations.items():
                existing = observed.get(pod_uid)
                if existing is not None and existing != gpu_uuids:
                    raise RuntimeError("GPU observer found a conflicting Pod allocation")
                observed[pod_uid] = gpu_uuids
        await self._publish_observations(client, observed, observed_at=observed_at)
        return len(observed)

    async def _observe_namespace(
        self,
        client: httpx.AsyncClient,
        allocations: Mapping[str, tuple[str, ...]],
        *,
        namespace: str,
    ) -> dict[str, tuple[str, ...]]:
        response = await client.get(
            f"/api/v1/namespaces/{namespace}/pods",
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

        observed: dict[str, tuple[str, ...]] = {}
        for raw_pod in pods:
            pod = _mapping(raw_pod)
            metadata = _mapping(pod.get("metadata"))
            labels = _mapping(metadata.get("labels"))
            pod_uid = metadata.get("uid")
            if (
                not _has_unambiguous_model_label(labels)
                or not isinstance(pod_uid, str)
            ):
                continue
            gpu_uuids = allocations.get(pod_uid)
            gpu_count = pod_gpu_count(pod)
            if gpu_uuids is None or gpu_count is None or len(gpu_uuids) != gpu_count:
                continue
            observed[pod_uid] = gpu_uuids
        return observed

    async def _publish_observations(
        self,
        client: httpx.AsyncClient,
        allocations: Mapping[str, tuple[str, ...]],
        *,
        observed_at: datetime,
    ) -> None:
        name = allocation_config_map_name(self.node_name)
        path = f"/api/v1/namespaces/{self.publication_namespace}/configmaps/{name}"
        response = await client.get(path)
        if response.status_code not in {200, 404}:
            raise RuntimeError("GPU observer publication lookup failed")
        resource_version: str | None = None
        prior = {}
        if response.status_code == 200:
            if len(response.content) > MAX_POD_LIST_BYTES:
                raise RuntimeError("GPU observer publication is too large")
            try:
                existing = _mapping(response.json())
            except (json.JSONDecodeError, UnicodeDecodeError, RecursionError):
                raise RuntimeError("GPU observer publication is invalid") from None
            metadata = _mapping(existing.get("metadata"))
            labels = _mapping(metadata.get("labels"))
            resource_version = metadata.get("resourceVersion")
            if (
                labels.get("app.kubernetes.io/component") != "gpu-allocation-observer"
                or not isinstance(resource_version, str)
            ):
                raise RuntimeError("GPU observer publication has different ownership")
            try:
                prior = parse_observations(existing, expected_node_name=self.node_name)
            except ValueError:
                raise RuntimeError("GPU observer publication contract is invalid") from None
        metadata: dict[str, Any] = {
            "name": name,
            "namespace": self.publication_namespace,
            "labels": {"app.kubernetes.io/component": "gpu-allocation-observer"},
        }
        if resource_version is not None:
            metadata["resourceVersion"] = resource_version
        body = {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": metadata,
            "data": {
                DATA_KEY: encode_observations(
                    node_name=self.node_name,
                    allocations=allocations,
                    observed_at=observed_at,
                    resolution_seconds=self.poll_seconds,
                    prior=prior,
                )
            },
        }
        if resource_version is None:
            published = await client.post(
                f"/api/v1/namespaces/{self.publication_namespace}/configmaps",
                json=body,
            )
            if published.status_code not in {201, 409}:
                raise RuntimeError("GPU observer publication create failed")
        else:
            published = await client.put(path, json=body)
            if published.status_code != 200:
                raise RuntimeError("GPU observer publication update failed")


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
