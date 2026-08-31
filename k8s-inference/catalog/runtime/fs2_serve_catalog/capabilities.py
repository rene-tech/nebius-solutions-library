#!/usr/bin/env python3
"""Fail-closed live backend capabilities consumed by model renderers."""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Mapping

from .loader import (
    CatalogError,
    FederatedBackend,
    ModelRecord,
    _enum,
    _exact,
    _list,
    _positive_int,
    _text,
    strong_sha256,
)


BACKEND_CAPABILITY_SCHEMA = "fs2-serve.nebius.ai/backend-capability/v6"
LOCAL_PV_PVC_SCHEMA = "fs2-serve.nebius.ai/local-pv-pvc-lifecycle/v1"
PROVIDER_BLOCK_PVC_SCHEMA = "fs2-serve.nebius.ai/provider-block-pvc-lifecycle/v2"
GPU_TOLERATION = {
    "key": "dedicated",
    "operator": "Equal",
    "value": "fs2-inference",
    "effect": "NoSchedule",
}
LOCAL_GPU_CLASS = "NVIDIA-B300-SXM6-288GB"
SM90_GPU_CLASSES = {"NVIDIA-H100-SXM-80GB", "NVIDIA-H200-SXM"}
REGION = re.compile(r"^[a-z]+-[a-z]+[0-9]+$")
NIM_TAG = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$")
K8S_UID = re.compile(r"^[0-9a-f]{8}-[0-9a-f-]{27,}$")


@dataclass(frozen=True)
class BackendCapability:
    """A model-bound deployment target; it is never route authority by itself."""

    backend_id: str
    backend_class: str
    admission_scope: str
    gpu_class: str
    node_gpu_count: int
    workload_gpu_count: int
    storage_mode: str | None
    runtime_tuple_digest: str | None
    _value: Mapping[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return copy.deepcopy(dict(self._value))

    @property
    def node_selector(self) -> dict[str, str]:
        scheduling = self._value["scheduling"]
        return copy.deepcopy(scheduling["node_selector"] if scheduling else {})

    @property
    def tolerations(self) -> list[dict[str, Any]]:
        scheduling = self._value["scheduling"]
        return copy.deepcopy(scheduling["tolerations"] if scheduling else [])

    @property
    def storage(self) -> dict[str, Any] | None:
        value = self._value["storage"]
        return copy.deepcopy(value) if value is not None else None

    @property
    def nim_image(self) -> dict[str, Any] | None:
        value = self._value["nim_image"]
        return copy.deepcopy(value) if value is not None else None

    @property
    def node_identity(self) -> dict[str, Any] | None:
        storage = self._value["storage"]
        if storage is None or storage["node_identity"] is None:
            return None
        return copy.deepcopy(storage["node_identity"])

    @property
    def local_pv_pvc(self) -> dict[str, Any] | None:
        storage = self._value["storage"]
        if storage is None or storage["local_pv_pvc"] is None:
            return None
        return copy.deepcopy(storage["local_pv_pvc"])

    @property
    def provider_block_pvc(self) -> dict[str, Any] | None:
        storage = self._value["storage"]
        if storage is None or storage["provider_block_pvc"] is None:
            return None
        return copy.deepcopy(storage["provider_block_pvc"])


def _validate_provider_block_pvc(value: Any, record: ModelRecord) -> dict[str, Any]:
    lifecycle = _exact(
        value,
        {"schema", "state", "lifecycle_receipt_digest", "storage_class", "claim"},
        "provider block PVC lifecycle",
    )
    if lifecycle["schema"] != PROVIDER_BLOCK_PVC_SCHEMA or lifecycle["state"] != "verified":
        raise CatalogError("provider block PVC lifecycle is not verified")
    strong_sha256(lifecycle["lifecycle_receipt_digest"], "provider block lifecycle receipt")
    storage_class = validate_provider_block_storage_class_observation(
        lifecycle["storage_class"]
    )
    static = record.to_dict()["resources"]["gpu"]["placement"]["provider_block_pvc"]
    expected_class = static["storage_class"]
    if storage_class["metadata"]["name"] != expected_class["name"]:
        raise CatalogError("provider block StorageClass differs from the protected contract")
    claim = _exact(
        lifecycle["claim"],
        {
            "namespace",
            "name",
            "uid",
            "resource_version",
            "volume_name",
            "capacity_bytes",
            "access_modes",
            "volume_mode",
            "fs_type",
        },
        "provider block PVC identity",
    )
    static_claim = static["claim"]
    if (
        claim["namespace"] != static_claim["namespace"]
        or claim["name"] != static_claim["name"]
        or K8S_UID.fullmatch(_text(claim["uid"], "provider block PVC UID") or "") is None
        or not _text(claim["resource_version"], "provider block PVC resourceVersion")
        or not _text(claim["volume_name"], "provider block volume name")
        or claim["capacity_bytes"] < static_claim["requested_bytes"]
        or claim["access_modes"] != ["ReadWriteOnce"]
        or claim["volume_mode"] != "Filesystem"
        or claim["fs_type"] != "ext4"
    ):
        raise CatalogError("provider block PVC identity differs from the exact retained claim")
    return lifecycle


def validate_provider_block_storage_class_observation(value: Any) -> dict[str, Any]:
    """Validate an exact, signed Kubernetes API-server StorageClass observation."""

    observation = _exact(
        value,
        {"apiVersion", "kind", "metadata", "spec"},
        "provider block StorageClass API observation",
    )
    metadata = _exact(
        observation["metadata"],
        {"name", "uid", "resourceVersion"},
        "provider block StorageClass metadata",
    )
    spec = _exact(
        observation["spec"],
        {
            "provisioner",
            "reclaimPolicy",
            "volumeBindingMode",
            "allowVolumeExpansion",
            "parameters",
        },
        "provider block StorageClass spec",
    )
    parameters = _exact(
        spec["parameters"],
        {"type", "csi.storage.k8s.io/fstype"},
        "provider block StorageClass parameters",
    )
    uid = _text(metadata["uid"], "provider block StorageClass UID")
    resource_version = _text(
        metadata["resourceVersion"], "provider block StorageClass resourceVersion"
    )
    if (
        observation["apiVersion"] != "storage.k8s.io/v1"
        or observation["kind"] != "StorageClass"
        or metadata["name"] != "fs2-network-ssd-retain"
        or uid is None
        or K8S_UID.fullmatch(uid) is None
        or resource_version is None
        or spec["provisioner"] != "compute.csi.nebius.com"
        or spec["reclaimPolicy"] != "Retain"
        or spec["volumeBindingMode"] != "WaitForFirstConsumer"
        or spec["allowVolumeExpansion"] is not True
        or parameters
        != {
            "type": "NETWORK_SSD",
            "csi.storage.k8s.io/fstype": "ext4",
        }
    ):
        raise CatalogError(
            "provider block StorageClass is not the exact server-observed Retain Compute CSI contract"
        )
    return observation


def _strings(value: Any, label: str) -> list[str]:
    result = _list(value, label, nonempty=True)
    if result != sorted(result) or len(result) != len(set(result)):
        raise CatalogError(f"{label} must be sorted and unique")
    for item in result:
        _text(item, label)
    return result


def _validate_scheduling(value: Any, gpu: Mapping[str, Any]) -> dict[str, Any]:
    scheduling = _exact(
        value,
        {"pool", "capacity_type", "node_selector", "tolerations"},
        "backend scheduling",
    )
    pool = _enum(
        scheduling["pool"],
        {"b300-hot-8x", "b300-burst-8x", "b300-burst-1x"},
        "backend scheduling pool",
    )
    capacity_type = _enum(
        scheduling["capacity_type"],
        {"regular", "preemptible"},
        "backend capacity type",
    )
    selector = scheduling["node_selector"]
    if not isinstance(selector, dict) or not selector:
        raise CatalogError("backend scheduling requires an exact non-empty node selector")
    if any(
        not isinstance(key, str)
        or not key
        or not isinstance(item, str)
        or not item
        for key, item in selector.items()
    ):
        raise CatalogError("backend node selector must contain non-empty strings")
    if list(selector) != sorted(selector):
        raise CatalogError("backend node selector must be canonically sorted")
    tolerations = _list(scheduling["tolerations"], "backend tolerations", nonempty=True)
    if tolerations != [GPU_TOLERATION]:
        raise CatalogError("backend must bind the exact fs2 GPU taint toleration")

    node_count = gpu["node_count"]
    expected = {
        "workload.fs2.nebius/gpu": "true",
        "capacity.fs2.nebius/type": capacity_type,
        "capacity.fs2.nebius/gpu-count": str(node_count),
    }
    expected_preset = "b300-1x" if node_count == 1 else "b300-8x"
    if gpu["node_preset"] != expected_preset:
        raise CatalogError("backend node preset differs from its physical GPU count")
    expected["capacity.fs2.nebius/preset"] = expected_preset
    expected["capacity.fs2.nebius/pool"] = "hot" if pool == "b300-hot-8x" else "burst"
    extra_keys = set(selector) - set(expected)
    if extra_keys not in (set(), {"kubernetes.io/hostname"}) or any(
        selector.get(key) != item for key, item in expected.items()
    ):
        raise CatalogError("backend selector differs from the exact cluster-owned GPU pool labels")
    if pool == "b300-hot-8x" and (capacity_type != "regular" or node_count != 8):
        raise CatalogError("hot B300 pool must be regular eight-GPU capacity")
    if pool == "b300-burst-8x" and (capacity_type != "preemptible" or node_count != 8):
        raise CatalogError("burst B300 8x pool must be preemptible eight-GPU capacity")
    if pool == "b300-burst-1x" and (capacity_type != "preemptible" or node_count != 1):
        raise CatalogError("burst B300 1x pool must be preemptible one-GPU capacity")
    return scheduling


def _validate_local_pv_pvc(
    value: Any,
    record: ModelRecord,
    scheduling: Mapping[str, Any],
    node_identity: Mapping[str, Any],
) -> dict[str, Any]:
    lifecycle = _exact(
        value,
        {
            "schema",
            "state",
            "lifecycle_receipt_digest",
            "cache_namespace",
            "localizer_security_profile",
            "storage_class_name",
            "volume_binding_mode",
            "activation_generation",
            "persistent_volume",
            "persistent_volume_claim",
            "activation_target",
            "fencing",
        },
        "local-PV/PVC lifecycle",
    )
    if lifecycle["schema"] != LOCAL_PV_PVC_SCHEMA or lifecycle["state"] != "reviewed-implemented":
        raise CatalogError("local-PV/PVC lifecycle is not reviewed and implemented")
    strong_sha256(
        lifecycle["lifecycle_receipt_digest"], "local-PV/PVC lifecycle receipt"
    )
    if (
        lifecycle["cache_namespace"] != "fs2-models"
        or lifecycle["localizer_security_profile"] != "restricted-unprivileged"
        or lifecycle["storage_class_name"] != "fs2-local-nvme"
        or lifecycle["volume_binding_mode"] != "WaitForFirstConsumer"
    ):
        raise CatalogError("local-PV/PVC lifecycle violates the reviewed storage/PSS boundary")
    activation_generation = _positive_int(
        lifecycle["activation_generation"], "local-PV/PVC activation generation"
    )
    assert activation_generation is not None
    persistent_volume = _exact(
        lifecycle["persistent_volume"],
        {"name", "uid", "resource_version", "node_affinity"},
        "local PersistentVolume identity",
    )
    persistent_volume_claim = _exact(
        lifecycle["persistent_volume_claim"],
        {
            "namespace",
            "name",
            "uid",
            "resource_version",
            "volume_name",
            "access_modes",
        },
        "local PersistentVolumeClaim identity",
    )
    activation_target = _exact(
        lifecycle["activation_target"],
        {"api_version", "kind", "namespace", "name", "uid"},
        "local-PV/PVC activation target",
    )
    fencing = _exact(
        lifecycle["fencing"],
        {
            "preemption",
            "lost_node",
            "activation_generation_recreation",
        },
        "local-PV/PVC fencing",
    )
    for label, item in (
        ("PersistentVolume name", persistent_volume["name"]),
        ("PersistentVolume resourceVersion", persistent_volume["resource_version"]),
        ("PersistentVolumeClaim name", persistent_volume_claim["name"]),
        (
            "PersistentVolumeClaim resourceVersion",
            persistent_volume_claim["resource_version"],
        ),
    ):
        _text(item, label)
    for label, item in (
        ("PersistentVolume UID", persistent_volume["uid"]),
        ("PersistentVolumeClaim UID", persistent_volume_claim["uid"]),
        ("activation target UID", activation_target["uid"]),
    ):
        uid = _text(item, label)
        if uid is None or K8S_UID.fullmatch(uid) is None:
            raise CatalogError(f"{label} is not an exact Kubernetes UID")
    if persistent_volume_claim["namespace"] != "fs2-models":
        raise CatalogError("local model PVC must be namespaced with its model Pod")
    if persistent_volume_claim["volume_name"] != persistent_volume["name"]:
        raise CatalogError("local PVC is not bound to the exact local PV")
    if persistent_volume_claim["access_modes"] != ["ReadWriteOnce"]:
        raise CatalogError("local model PVC must be exact ReadWriteOnce storage")
    node_affinity = _exact(
        persistent_volume["node_affinity"],
        {"node_name", "node_uid", "required_node_selector"},
        "local PersistentVolume nodeAffinity",
    )
    if (
        node_affinity["node_name"] != node_identity["name"]
        or node_affinity["node_uid"] != node_identity["uid"]
        or node_affinity["required_node_selector"] != scheduling["node_selector"]
    ):
        raise CatalogError("local PersistentVolume nodeAffinity differs from the exact serving Node")
    if activation_target != {
        "api_version": "apps/v1",
        "kind": "Deployment",
        "namespace": "fs2-models",
        "name": record.model_id,
        "uid": activation_target["uid"],
    }:
        raise CatalogError("local PVC activation target differs from the model Deployment")
    if fencing != {
        "preemption": "pod-pvc-pv-node-uid",
        "lost_node": "invalidate-and-recreate-next-activation-generation",
        "activation_generation_recreation": True,
    }:
        raise CatalogError("local-PV/PVC preemption or lost-node fencing is incomplete")
    return lifecycle


def _validate_storage(
    value: Any,
    record: ModelRecord,
    pool: str,
    scheduling: Mapping[str, Any],
) -> str:
    storage = _exact(
        value,
        {
            "mode",
            "pvc_requirement_id",
            "mount_path",
            "node_identity",
            "provider_block_pvc",
            "local_pv_pvc",
        },
        "backend storage",
    )
    mode = _enum(
        storage["mode"],
        {"provider-block-pvc", "sfs-pvc", "local-nvme", "nimcache-pvc"},
        "backend storage mode",
    )
    pvc_id = _text(storage["pvc_requirement_id"], "backend PVC requirement", nullable=True)
    mount_path = _text(storage["mount_path"], "backend storage mount path")
    node_identity = storage["node_identity"]
    provider_block_pvc = storage["provider_block_pvc"]
    local_pv_pvc = storage["local_pv_pvc"]
    record_value = record.to_dict()
    if mode == "local-nvme":
        if pool == "b300-burst-1x":
            raise CatalogError("B300 1x is SFS/conventional-only and cannot use local NVMe")
        placement = record_value["resources"]["gpu"]["placement"]
        if (
            placement is None
            or placement["local_pv_pvc"]["state"] != "reviewed-implemented"
            or "node-local-pv-pvc-qualified" not in placement["cache_capabilities"]
        ):
            raise CatalogError("local-PV/PVC lifecycle remains gated-unimplemented")
        if pvc_id is not None:
            raise CatalogError("local model PVC identity must come from the reviewed lifecycle")
        if provider_block_pvc is not None:
            raise CatalogError("node-local storage cannot carry a provider block PVC identity")
        if mount_path != record_value["cache"]["local_path"]:
            raise CatalogError("local NVMe mount path differs from the catalog")
        node = _exact(
            node_identity,
            {"name", "uid", "provider_id_sha256"},
            "local NVMe serving node identity",
        )
        name = _text(node["name"], "local NVMe serving node name")
        uid = _text(node["uid"], "local NVMe serving node UID")
        if (
            name is None
            or len(name) > 253
            or uid is None
            or K8S_UID.fullmatch(uid) is None
        ):
            raise CatalogError("local NVMe requires an exact serving node name and UID")
        strong_sha256(
            node["provider_id_sha256"], "local NVMe serving node provider identity"
        )
        _validate_local_pv_pvc(local_pv_pvc, record, scheduling, node)
    elif mode == "provider-block-pvc":
        placement = record_value["resources"]["gpu"]["placement"]
        if record.model_id != "qwen3-8b" or placement is None:
            raise CatalogError("provider block PVC is currently reviewed only for Qwen")
        if pvc_id is not None or node_identity is not None or local_pv_pvc is not None:
            raise CatalogError("provider block PVC cannot substitute prerequisite or local storage")
        if mount_path != "/mnt/fs2-provider-block":
            raise CatalogError("provider block PVC mount path is not canonical")
        if "kubernetes.io/hostname" in scheduling["node_selector"]:
            raise CatalogError("portable provider block storage cannot pin an exact Node")
        if provider_block_pvc is None:
            raise CatalogError("provider block backend lacks its exact live claim identity")
        _validate_provider_block_pvc(provider_block_pvc, record)
    else:
        if pvc_id != "fs2-models/shared-cache-pvc":
            raise CatalogError("SFS-backed storage requires only the bound shared-cache PVC")
        if mount_path != "/mnt/fs2-serve-cache":
            raise CatalogError("SFS-backed storage must use the canonical shared mount")
        if node_identity is not None:
            raise CatalogError("shared storage cannot claim a node-local serving identity")
        if local_pv_pvc is not None:
            raise CatalogError("shared storage cannot claim a local-PV/PVC lifecycle")
        if provider_block_pvc is not None:
            raise CatalogError("shared storage cannot claim a provider block PVC lifecycle")
        if "kubernetes.io/hostname" in scheduling["node_selector"]:
            raise CatalogError("shared storage cannot pin an exact serving Node")
    if mode == "nimcache-pvc" and record_value["runtime"]["kind"] != "nim":
        raise CatalogError("NIMCache storage is valid only for a NIM runtime")
    if mode != "nimcache-pvc" and record_value["cache"]["owner"] == "nim-operator-nimcache":
        raise CatalogError("NIM Operator cache ownership requires nimcache-pvc storage")
    return mode


def bind_backend_capability(
    record: ModelRecord,
    value: Any,
    *,
    federated_backend: FederatedBackend | None = None,
) -> BackendCapability:
    """Bind one exact local B300 or federated SM90 target to a model identity."""

    document = _exact(
        value,
        {
            "schema",
            "backend_id",
            "backend_class",
            "region",
            "admission_scope",
            "model_id",
            "model_digest",
            "model_revision",
            "runtime_image_digest",
            "gpu",
            "allowed_mechanisms",
            "scheduling",
            "storage",
            "runtime_tuple_digest",
            "backend_identity_digest",
            "nim_image",
        },
        "backend capability",
    )
    if document["schema"] != BACKEND_CAPABILITY_SCHEMA:
        raise CatalogError("unsupported backend capability schema")
    backend_id = _text(document["backend_id"], "backend capability ID")
    assert backend_id is not None
    backend_class = _enum(
        document["backend_class"],
        {"local-kubernetes", "federated-upstream"},
        "backend class",
    )
    region = _text(document["region"], "backend region")
    if region is None or REGION.fullmatch(region) is None:
        raise CatalogError("backend region is not canonical")
    admission_scope = _enum(
        document["admission_scope"],
        {"experiment-only", "route-qualified"},
        "backend admission scope",
    )
    record_value = record.to_dict()
    expected = {
        "model_id": record.model_id,
        "model_digest": record.digest,
        "model_revision": record_value["model"]["source"]["revision"],
        "runtime_image_digest": record_value["runtime"]["image"]["digest"],
    }
    if any(document[key] != item for key, item in expected.items()):
        raise CatalogError("backend capability differs from the immutable model subject")
    strong_sha256(document["model_digest"], "backend model digest")
    strong_sha256(document["runtime_image_digest"], "backend runtime digest", image=True)
    strong_sha256(document["backend_identity_digest"], "backend identity digest")

    gpu = _exact(
        document["gpu"],
        {
            "class",
            "node_preset",
            "node_count",
            "node_topology",
            "workload_count",
            "workload_topology",
        },
        "backend GPU",
    )
    gpu_class = _enum(
        gpu["class"], {LOCAL_GPU_CLASS, *SM90_GPU_CLASSES}, "backend GPU class"
    )
    _text(gpu["node_preset"], "backend node preset")
    node_count = _positive_int(gpu["node_count"], "backend node GPU count")
    workload_count = _positive_int(gpu["workload_count"], "backend workload GPU count")
    assert node_count is not None and workload_count is not None
    _text(gpu["node_topology"], "backend node topology")
    if (
        workload_count != record_value["resources"]["gpu"]["count"]
        or gpu["workload_topology"] != record_value["resources"]["gpu"]["topology"]
    ):
        raise CatalogError("backend workload GPU topology differs from the model record")
    if workload_count > node_count:
        raise CatalogError("backend workload requests more GPUs than the selected node")
    mechanisms = _strings(document["allowed_mechanisms"], "allowed mechanisms")
    if "conventional" not in mechanisms:
        raise CatalogError("every backend capability must retain conventional fallback")
    unknown_mechanisms = set(mechanisms) - {
        "conventional",
        "snapshot",
        "sleep-wake",
        "custom-runtime",
    }
    if unknown_mechanisms:
        raise CatalogError("backend capability contains an unknown startup mechanism")

    scheduling = document["scheduling"]
    storage = document["storage"]
    runtime_tuple = document["runtime_tuple_digest"]
    nim_image = document["nim_image"]
    if backend_class == "local-kubernetes":
        if gpu_class != LOCAL_GPU_CLASS:
            raise CatalogError("the local deployment contract currently admits only B300")
        b300_state = record_value["resources"]["gpu"]["b300_state"]
        if b300_state in {"incompatible-sm103", "blocked"}:
            raise CatalogError("SM103-incompatible or blocked model cannot bind a B300 target")
        if admission_scope == "route-qualified" and b300_state != "qualified":
            raise CatalogError("route-qualified B300 target requires qualified model support")
        strong_sha256(runtime_tuple, "backend runtime tuple digest")
        scheduling_value = _validate_scheduling(scheduling, gpu)
        pool = scheduling_value["pool"]
        storage_mode = _validate_storage(storage, record, pool, scheduling_value)
        placement = record_value["resources"]["gpu"]["placement"]
        if admission_scope == "route-qualified" and placement is None:
            raise CatalogError("route-qualified B300 target requires a reviewed model placement")
        if placement is not None:
            if (
                gpu["node_preset"] != placement["node_preset"]
                or node_count != placement["node_gpu_count"]
                or pool != placement["pool"]
                or scheduling_value["capacity_type"] != placement["capacity_type"]
                or scheduling_value["tolerations"] != placement["tolerations"]
            ):
                raise CatalogError("backend target differs from the model node placement")
            for key, item in placement["node_selector"].items():
                if scheduling_value["node_selector"].get(key) != item:
                    raise CatalogError("backend selector differs from the model placement")
            capability_name = (
                "node-local-pv-pvc-qualified"
                if storage_mode == "local-nvme"
                else "provider-block-pvc-qualified"
                if storage_mode == "provider-block-pvc" and admission_scope == "route-qualified"
                else "provider-block-pvc-candidate"
                if storage_mode == "provider-block-pvc"
                else "sfs-conventional-qualified"
            )
            if capability_name not in placement["cache_capabilities"]:
                raise CatalogError("backend storage is not admitted by the model placement")
        if storage_mode == "local-nvme" and scheduling_value["node_selector"].get(
            "kubernetes.io/hostname"
        ) != storage["node_identity"]["name"]:
            raise CatalogError(
                "node-local backend selector differs from the exact serving Node"
            )
        if pool == "b300-burst-1x" and mechanisms != ["conventional"]:
            raise CatalogError("B300 1x admits only conventional startup")
        if "snapshot" in mechanisms and (node_count != 8 or storage_mode != "local-nvme"):
            raise CatalogError("snapshot experiments require an NVMe-capable B300 8x node")
        if record_value["runtime"]["kind"] == "nim":
            image = _exact(
                nim_image,
                {"repository", "tag", "expected_digest", "tag_binding_receipt_digest"},
                "NIM image binding",
            )
            if image["repository"] != record_value["model"]["source"]["repository"]:
                raise CatalogError("NIM tag binding names a different repository")
            tag = _text(image["tag"], "NIM image tag")
            if tag is None or NIM_TAG.fullmatch(tag) is None:
                raise CatalogError("NIM image tag is not canonical")
            if image["expected_digest"] != document["runtime_image_digest"]:
                raise CatalogError("NIM tag binding names a different image digest")
            strong_sha256(image["tag_binding_receipt_digest"], "NIM tag binding receipt")
        elif nim_image is not None:
            raise CatalogError("non-NIM backend capability cannot carry a NIM tag binding")
    else:
        if gpu_class not in SM90_GPU_CLASSES:
            raise CatalogError("federated upstreams are limited to exact H100/H200 subjects")
        if any(item is not None for item in (scheduling, storage, runtime_tuple, nim_image)):
            raise CatalogError("federated backend cannot invent local scheduling or storage")
        if federated_backend is None:
            raise CatalogError("federated capability requires the packaged exact inventory")
        inventory = federated_backend.to_dict()
        if (
            federated_backend.model_id != record.model_id
            or inventory["gpu_class"] != gpu_class
            or inventory["runtime_image_digest"] != document["runtime_image_digest"]
            or inventory["backend_class"] != backend_id
        ):
            raise CatalogError("federated capability differs from the exact SM90 inventory")
        if admission_scope == "route-qualified" and inventory["route_state"] != "qualified":
            raise CatalogError("federated route is not qualified in the packaged inventory")
        storage_mode = None

    return BackendCapability(
        backend_id=backend_id,
        backend_class=backend_class,
        admission_scope=admission_scope,
        gpu_class=gpu_class,
        node_gpu_count=node_count,
        workload_gpu_count=workload_count,
        storage_mode=storage_mode,
        runtime_tuple_digest=runtime_tuple,
        _value=MappingProxyType(copy.deepcopy(document)),
    )


def require_local_capability(
    record: ModelRecord,
    capability: BackendCapability,
    *,
    storage_modes: set[str] | None = None,
    mechanism: str = "conventional",
) -> None:
    """Check a previously bound capability at each workload emission boundary."""

    value = capability.to_dict()
    record_value = record.to_dict()
    if (
        capability.backend_class != "local-kubernetes"
        or value["model_id"] != record.model_id
        or value["model_digest"] != record.digest
        or value["model_revision"] != record_value["model"]["source"]["revision"]
        or value["runtime_image_digest"] != record_value["runtime"]["image"]["digest"]
    ):
        raise CatalogError("workload renderer requires the model-bound local backend capability")
    if mechanism not in value["allowed_mechanisms"]:
        raise CatalogError("backend capability does not admit the requested startup mechanism")
    if storage_modes is not None and capability.storage_mode not in storage_modes:
        raise CatalogError("backend storage mode is incompatible with this workload")
