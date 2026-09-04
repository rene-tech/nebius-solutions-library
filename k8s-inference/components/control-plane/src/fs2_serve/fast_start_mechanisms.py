"""Cold-start optimisation mechanisms behind the customer-facing fast-start levels.

``fast_start.py`` owns what a level *means* and refuses to grant one without
20 comparable failure-free samples at p95 for the exact deployment tuple.  This
module owns the other half: naming the mechanisms that can make a cold start
faster, and reporting which of them a given accelerator pool can actually
provide.

Nothing here can raise a level.  A mechanism is operator detail; the level
comes from evidence alone.

Each mechanism is also projected into ModelDeployment status, where a
``Configured`` mechanism sits next to whatever level the evidence supports,
which is ``Off`` until a cohort is populated.

``regional-cache`` costs no reserved capacity.  ``host-memory-residency``
trades an explicit host RAM reservation for start latency, so its price is
scheduled and attributable rather than an incidental page-cache effect another
workload can evict.  ``gpu-resident`` parks a warm engine in GPU memory so
activation is a promotion instead of a load, which costs an accelerator for as
long as the replica is parked; the declaration states the hot floor it depends
on and validation refuses an implicit one.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal

from pydantic import Field, model_validator

from .models import KubernetesModel

SHA256_DIGEST_PATTERN = r"^sha256:[a-f0-9]{64}$"
IMAGE_DIGEST_PATTERN = r"^[^\s@]+@sha256:[a-f0-9]{64}$"
DNS_SUBDOMAIN_PATTERN = r"^[a-z0-9](?:[a-z0-9-]{0,251}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,251}[a-z0-9])?)*$"
CONTENT_PATH_PATTERN = r"^/(?:[A-Za-z0-9._-]+/?)+$"
MECHANISM_NAME_PATTERN = r"^[a-z][a-z0-9-]*$"
REASON_PATTERN = r"^[A-Za-z][A-Za-z0-9]*$"


# Pool capability is read from the exact node selector Terraform renders for the
# pool, so an unavailable mechanism is proved by the same value the scheduler
# uses rather than by a separate hand-maintained capability list.
LOCAL_NVME_ELIGIBLE_LABEL = "local-nvme.fs2.nebius/eligible"
SNAPSHOT_ELIGIBLE_LABEL = "snapshot.fs2.nebius/eligible"

# Mechanism-owned Pod annotations.
MECHANISM_ANNOTATION = "fast-start.fs2.nebius/mechanism"
MECHANISM_CONFIG_DIGEST_ANNOTATION = "fast-start.fs2.nebius/mechanism-config-digest"
MECHANISM_RESERVED_MEMORY_ANNOTATION = "fast-start.fs2.nebius/reserved-host-memory-bytes"
MECHANISM_STANDBY_ANNOTATION = "fast-start.fs2.nebius/gpu-resident-standby-replicas"
MECHANISM_HOT_FLOOR_ANNOTATION = "fast-start.fs2.nebius/gpu-resident-minimum-hot-replicas"
HOST_MEMORY_RESIDENCY_LABEL = "fast-start.fs2.nebius/host-memory-residency"
HOST_MEMORY_RESIDENCY_HOLDER_LABEL = "fast-start.fs2.nebius/host-memory-holder"
GPU_RESIDENT_ROLE_LABEL = "fast-start.fs2.nebius/gpu-resident-role"
GPU_RESIDENT_READINESS_GATE = "fast-start.fs2.nebius/promoted"

MINIMUM_RESIDENCY_HEADROOM_BYTES = 256 * 1024 * 1024


class FastStartMechanismError(ValueError):
    """A mechanism declaration or render input is not usable."""


def canonical_json(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")


def canonical_digest(value: object) -> str:
    return f"sha256:{hashlib.sha256(canonical_json(value)).hexdigest()}"


class FastStartMechanism(StrEnum):
    """Every mechanism name the platform recognises.

    These are operator detail.  None of them is a customer level and none of
    them creates a level above ``L4``.
    """

    CONVENTIONAL = "conventional"
    REGIONAL_CACHE = "regional-cache"
    HOST_MEMORY_RESIDENCY = "host-memory-residency"
    GPU_RESIDENT = "gpu-resident"
    SHARED_RESTORE = "shared-restore"
    NODE_LOCAL_RESTORE = "node-local-restore"
    MODELEXPRESS = "modelexpress"


#: Mechanisms an operator may pin on a ``ModelDeployment`` through this module.
SELECTABLE_MECHANISMS: tuple[FastStartMechanism, ...] = (
    FastStartMechanism.CONVENTIONAL,
    FastStartMechanism.REGIONAL_CACHE,
    FastStartMechanism.HOST_MEMORY_RESIDENCY,
)

#: Mechanisms that need a declared per-model configuration before they render.
DECLARED_MECHANISMS: tuple[FastStartMechanism, ...] = (
    FastStartMechanism.REGIONAL_CACHE,
    FastStartMechanism.HOST_MEMORY_RESIDENCY,
    FastStartMechanism.GPU_RESIDENT,
)


class MechanismAvailability(KubernetesModel):
    """Whether one pool can physically run one mechanism, and why."""

    mechanism: str = Field(min_length=1, max_length=64, pattern=MECHANISM_NAME_PATTERN)
    state: Literal["Available", "Unavailable"]
    reason: str = Field(min_length=1, max_length=64, pattern=REASON_PATTERN)
    evidence_selector: dict[str, str] = Field(default_factory=dict, max_length=8)


def _selector_capability(node_selector: Mapping[str, str], label: str) -> tuple[bool, dict[str, str]]:
    """Return the pool's capability for ``label`` and the selector that proves it."""

    value = node_selector.get(label)
    if value is None:
        return False, {}
    return value == "true", {label: value}


def assess_pool_mechanisms(
    *,
    pool_id: str,
    node_selector: Mapping[str, str],
) -> list[MechanismAvailability]:
    """Report every mechanism's availability for one accelerator pool.

    Only the labels Terraform actually renders into the pool's scheduling
    selector are consulted, so an unavailable mechanism is proved by the same
    value the scheduler uses.  The retained H100 pool selects
    ``local-nvme.fs2.nebius/eligible=false`` and
    ``snapshot.fs2.nebius/eligible=false``, so node-local restore and shared
    restore are reported unavailable with that exact selector attached.  They
    are never attempted and never silently downgraded to another path.

    The production cache and host-memory mechanisms need retained regional
    storage rather than a node capability, which is a per-model storage
    contract; pool-level availability therefore says only that the pool can
    host them. GPU residency remains unavailable until a production controller
    owns the promotion readiness condition.
    """

    if not pool_id:
        raise FastStartMechanismError("pool identity is required to assess mechanisms")
    local_nvme, local_nvme_selector = _selector_capability(node_selector, LOCAL_NVME_ELIGIBLE_LABEL)
    snapshot, snapshot_selector = _selector_capability(node_selector, SNAPSHOT_ELIGIBLE_LABEL)
    results = [
        MechanismAvailability(
            mechanism=FastStartMechanism.CONVENTIONAL.value,
            state="Available",
            reason="ConventionalLoaderAlwaysAvailable",
        ),
        MechanismAvailability(
            mechanism=FastStartMechanism.REGIONAL_CACHE.value,
            state="Available",
            reason="RegionalRetainedCacheSupported",
        ),
        MechanismAvailability(
            mechanism=FastStartMechanism.HOST_MEMORY_RESIDENCY.value,
            state="Available",
            reason="HostMemoryResidencySupported",
        ),
        MechanismAvailability(
            mechanism=FastStartMechanism.GPU_RESIDENT.value,
            state="Unavailable",
            reason="PromotionControllerNotInstalled",
        ),
        MechanismAvailability(
            mechanism=FastStartMechanism.SHARED_RESTORE.value,
            state="Available" if snapshot else "Unavailable",
            reason="SnapshotCapabilityPresent" if snapshot else "NoQualifiedSnapshotCapability",
            evidence_selector=snapshot_selector,
        ),
        MechanismAvailability(
            mechanism=FastStartMechanism.NODE_LOCAL_RESTORE.value,
            state="Available" if local_nvme else "Unavailable",
            reason="NodeLocalNvmePresent" if local_nvme else "NoNodeLocalNvme",
            evidence_selector=local_nvme_selector,
        ),
    ]
    return sorted(results, key=lambda item: item.mechanism)


def unavailable_mechanisms(
    *,
    pool_id: str,
    node_selector: Mapping[str, str],
) -> dict[str, MechanismAvailability]:
    """Return the unavailable mechanisms for one pool keyed by mechanism name."""

    return {
        item.mechanism: item
        for item in assess_pool_mechanisms(pool_id=pool_id, node_selector=node_selector)
        if item.state == "Unavailable"
    }


class _SelfDigestModel(KubernetesModel):
    """A declaration whose ``configDigest`` is its own canonical identity.

    A mechanism's configuration is material to how fast it starts, so the whole
    reviewed declaration is bound into the digest that benchmark evidence must
    match.  Changing any configuration field starts a new evidence cohort
    instead of inheriting the old cohort's percentile.
    """

    config_digest: str = Field(pattern=SHA256_DIGEST_PATTERN)

    @model_validator(mode="after")
    def self_digest_matches(self) -> Any:
        if self.config_digest != self.expected_config_digest():
            raise ValueError("configDigest does not match the canonical mechanism declaration")
        return self

    def expected_config_digest(self) -> str:
        payload = self.model_dump(mode="json", by_alias=True, exclude={"config_digest"})
        return canonical_digest(payload)


class RetainedCompileCache(KubernetesModel):
    """A JIT/compile cache retained across Pods instead of discarded.

    ``abi`` is the compile-cache compatibility identity (driver plus target
    architecture).  It is part of the sub-path so a driver or accelerator change
    cannot read a cache built by an incompatible stack.
    """

    claim_name: str = Field(min_length=1, max_length=253, pattern=DNS_SUBDOMAIN_PATTERN)
    sub_path: str = Field(min_length=1, max_length=200, pattern=r"^[A-Za-z0-9](?:[A-Za-z0-9._/-]*[A-Za-z0-9])?$")
    abi: str = Field(min_length=3, max_length=128, pattern=r"^[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?$")
    mount_path: str = Field(min_length=2, max_length=200, pattern=CONTENT_PATH_PATTERN)
    size_limit_bytes: int = Field(ge=1 << 20, le=1 << 42)

    @model_validator(mode="after")
    def abi_bound_subpath(self) -> RetainedCompileCache:
        if self.abi not in self.sub_path:
            raise ValueError("the retained compile-cache subPath must contain its compile-cache ABI")
        if ".." in self.sub_path or self.sub_path.startswith("/"):
            raise ValueError("the retained compile-cache subPath must be relative and free of parent traversal")
        return self


class WarmPageCacheReadAhead(KubernetesModel):
    """A bounded pre-read that leaves the retained payload pages warm."""

    workers: int = Field(ge=1, le=64)
    read_bytes_limit: int = Field(ge=1 << 20, le=1 << 42)
    timeout_seconds: int = Field(ge=1, le=1800)


class RegionalCacheQualification(_SelfDigestModel):
    """Immutable declaration of the in-region cache path for one model.

    This is configuration compatibility, not benchmark evidence.  It lets the
    renderer serve the runtime image from the in-region mirror, keep the
    immutable payload on the regional shared filesystem, retain the compile
    cache, and warm the payload pages.  A level still needs a receipt bound to
    ``configDigest``.
    """

    schema_id: Literal["fs2-serve.nebius.ai/fast-start-regional-cache/v1"] = Field(
        default="fs2-serve.nebius.ai/fast-start-regional-cache/v1",
        alias="schema",
    )
    image_mirror_registry: str = Field(min_length=3, max_length=253)
    payload_claim_name: str = Field(min_length=1, max_length=253, pattern=DNS_SUBDOMAIN_PATTERN)
    payload_content_path: str = Field(min_length=2, max_length=512, pattern=CONTENT_PATH_PATTERN)
    payload_bytes: int = Field(ge=1, le=1 << 46)
    compile_cache: RetainedCompileCache
    warm_page_cache: WarmPageCacheReadAhead | None = None
    pool_refs: list[str] = Field(min_length=1, max_length=32)

    @model_validator(mode="after")
    def exact_declaration(self) -> RegionalCacheQualification:
        if re.fullmatch(r"[a-z0-9][a-z0-9.-]*(?::[0-9]{1,5})?", self.image_mirror_registry) is None:
            raise ValueError("the regional image mirror must be one registry host")
        if len(set(self.pool_refs)) != len(self.pool_refs):
            raise ValueError("regional-cache pool references must be unique")
        if self.warm_page_cache is not None and self.warm_page_cache.read_bytes_limit > self.payload_bytes:
            raise ValueError("the warm page-cache read budget cannot exceed the retained payload size")
        return self


class ResidencyHolder(KubernetesModel):
    """The node-scoped workload that owns the resident host-memory bytes."""

    name: str = Field(min_length=1, max_length=253, pattern=DNS_SUBDOMAIN_PATTERN)
    namespace: str = Field(min_length=1, max_length=63, pattern=DNS_SUBDOMAIN_PATTERN)
    receipt_claim_name: str = Field(min_length=1, max_length=253, pattern=DNS_SUBDOMAIN_PATTERN)
    receipt_mount_path: str = Field(min_length=2, max_length=200, pattern=CONTENT_PATH_PATTERN)


class HostMemoryResidencyQualification(_SelfDigestModel):
    """Immutable declaration of the RAM offload path for one model.

    ``reservedBytes`` is requested *and* limited on the holder, so the node
    memory this mechanism costs is an explicit reservation an operator can see
    and bill, never an incidental page-cache side effect.
    """

    schema_id: Literal["fs2-serve.nebius.ai/fast-start-host-memory-residency/v1"] = Field(
        default="fs2-serve.nebius.ai/fast-start-host-memory-residency/v1",
        alias="schema",
    )
    residency_mode: Literal["locked-payload-residency", "mapped-payload-residency", "runtime-sleep-offload"]
    payload_claim_name: str = Field(min_length=1, max_length=253, pattern=DNS_SUBDOMAIN_PATTERN)
    payload_content_path: str = Field(min_length=2, max_length=512, pattern=CONTENT_PATH_PATTERN)
    payload_digest: str = Field(pattern=SHA256_DIGEST_PATTERN)
    payload_bytes: int = Field(ge=1, le=1 << 46)
    reserved_bytes: int = Field(ge=1, le=1 << 46)
    node_allocatable_bytes: int = Field(ge=1, le=1 << 48)
    holder: ResidencyHolder
    receipt_max_age_seconds: int = Field(ge=30, le=86400)
    pool_refs: list[str] = Field(min_length=1, max_length=32)

    @model_validator(mode="after")
    def explicit_ram_accounting(self) -> HostMemoryResidencyQualification:
        if len(set(self.pool_refs)) != len(self.pool_refs):
            raise ValueError("host-memory-residency pool references must be unique")
        if self.residency_mode == "runtime-sleep-offload":
            # A sleeping engine holds the weights in its own process, so the
            # reservation belongs to the runtime rather than to a holder.
            if self.reserved_bytes < self.payload_bytes:
                raise ValueError("a sleep-offload reservation must cover the offloaded weights")
        else:
            if self.reserved_bytes < self.payload_bytes + MINIMUM_RESIDENCY_HEADROOM_BYTES:
                raise ValueError("a residency reservation must cover the payload plus holder headroom")
        if self.reserved_bytes > self.node_allocatable_bytes:
            raise ValueError("a residency reservation cannot exceed declared node allocatable memory")
        return self

    @property
    def reserved_fraction_of_node(self) -> float:
        return self.reserved_bytes / self.node_allocatable_bytes


class GpuResidentQualification(_SelfDigestModel):
    """Immutable declaration of the GPU-resident standby path for one model.

    A parked standby replica keeps its warm engine and weights in GPU memory,
    so activation is a promotion.  It also occupies accelerators the whole time,
    which is why ``minimumHotReplicas`` states the hot floor the deployment must
    carry for the mechanism to be affordable, and validation refuses a
    deployment whose floor is lower.
    """

    schema_id: Literal["fs2-serve.nebius.ai/fast-start-gpu-resident/v1"] = Field(
        default="fs2-serve.nebius.ai/fast-start-gpu-resident/v1",
        alias="schema",
    )
    residency_mode: Literal["standby-engine", "warm-engine-hot-floor"]
    standby_replicas: int = Field(ge=1, le=64)
    accelerators_per_standby_replica: int = Field(ge=1, le=64)
    minimum_hot_replicas: int = Field(ge=0, le=10000)
    promotion_probe_period_seconds: int = Field(ge=1, le=60)
    pool_refs: list[str] = Field(min_length=1, max_length=32)

    @model_validator(mode="after")
    def explicit_hot_floor_relationship(self) -> GpuResidentQualification:
        if len(set(self.pool_refs)) != len(self.pool_refs):
            raise ValueError("gpu-resident pool references must be unique")
        if self.residency_mode == "warm-engine-hot-floor" and self.minimum_hot_replicas < 1:
            raise ValueError("a warm-engine hot floor needs at least one hot replica")
        return self

    @property
    def reserved_accelerators(self) -> int:
        return self.standby_replicas * self.accelerators_per_standby_replica


def _container(pod_spec: Mapping[str, Any], name: str | None) -> dict[str, Any]:
    containers = pod_spec.get("containers")
    if not isinstance(containers, list) or not containers:
        raise FastStartMechanismError("the rendered Pod must declare at least one container")
    if name is None:
        first = containers[0]
        if not isinstance(first, dict):
            raise FastStartMechanismError("the rendered runtime container must be an object")
        return first
    for item in containers:
        if isinstance(item, dict) and item.get("name") == name:
            return item
    raise FastStartMechanismError("the rendered Pod does not contain the named runtime container")


def _volumes(pod_spec: dict[str, Any]) -> list[dict[str, Any]]:
    volumes = pod_spec.setdefault("volumes", [])
    if not isinstance(volumes, list) or any(not isinstance(item, dict) for item in volumes):
        raise FastStartMechanismError("rendered Pod volumes must be a list of objects")
    return volumes


def _volume_mounts(container: dict[str, Any]) -> list[dict[str, Any]]:
    mounts = container.setdefault("volumeMounts", [])
    if not isinstance(mounts, list) or any(not isinstance(item, dict) for item in mounts):
        raise FastStartMechanismError("rendered container volumeMounts must be a list of objects")
    return mounts


def _replace_volume(pod_spec: dict[str, Any], volume: Mapping[str, Any]) -> None:
    volumes = _volumes(pod_spec)
    remaining = [item for item in volumes if item.get("name") != volume["name"]]
    remaining.append(dict(volume))
    pod_spec["volumes"] = remaining


def _annotate(metadata: dict[str, Any], values: Mapping[str, str]) -> None:
    annotations = metadata.setdefault("annotations", {})
    if not isinstance(annotations, dict):
        raise FastStartMechanismError("rendered Pod annotations must be an object")
    annotations.update(values)


def _label(metadata: dict[str, Any], values: Mapping[str, str]) -> None:
    labels = metadata.setdefault("labels", {})
    if not isinstance(labels, dict):
        raise FastStartMechanismError("rendered Pod labels must be an object")
    labels.update(values)


def _set_env(container: dict[str, Any], values: Mapping[str, str]) -> None:
    existing = container.setdefault("env", [])
    if not isinstance(existing, list) or any(not isinstance(item, dict) for item in existing):
        raise FastStartMechanismError("rendered container env must be a list of objects")
    managed = set(values)
    retained = [item for item in existing if item.get("name") not in managed]
    container["env"] = [*retained, *({"name": name, "value": value} for name, value in sorted(values.items()))]


def _mount_path_of(container: Mapping[str, Any], volume_name: str) -> str | None:
    mounts = container.get("volumeMounts")
    if not isinstance(mounts, list):
        return None
    for item in mounts:
        if isinstance(item, dict) and item.get("name") == volume_name:
            path = item.get("mountPath")
            return path if isinstance(path, str) else None
    return None


def payload_mount(
    pod_spec: Mapping[str, Any],
    container: Mapping[str, Any],
    claim_name: str,
) -> tuple[str, str] | None:
    """Locate the volume that exposes the declared retained payload.

    The volume is found by the claim the declaration names, not by a
    conventional volume name, so a template that mounts its payload under any
    name still works and a template that does not mount it at all fails loudly.
    """

    volumes = pod_spec.get("volumes")
    if not isinstance(volumes, list):
        return None
    for volume in volumes:
        if not isinstance(volume, dict):
            continue
        claim = volume.get("persistentVolumeClaim")
        if not isinstance(claim, dict) or claim.get("claimName") != claim_name:
            continue
        name = volume.get("name")
        if not isinstance(name, str):
            continue
        mounted_at = _mount_path_of(container, name)
        if mounted_at is not None:
            return name, mounted_at
    return None


WARM_PAGE_CACHE_SCRIPT = r"""
import concurrent.futures as futures
import json
import os
import sys
import time

root = os.environ["FS2_WARM_ROOT"]
budget = int(os.environ["FS2_WARM_BYTES_LIMIT"])
workers = int(os.environ["FS2_WARM_WORKERS"])
deadline = time.monotonic() + float(os.environ["FS2_WARM_TIMEOUT_SECONDS"])
chunk = 8 << 20

paths = []
for directory, _unused, names in os.walk(root):
    for name in sorted(names):
        candidate = os.path.join(directory, name)
        if os.path.islink(candidate) or not os.path.isfile(candidate):
            continue
        paths.append((candidate, os.path.getsize(candidate)))
paths.sort(key=lambda item: (-item[1], item[0]))

selected = []
planned = 0
for candidate, size in paths:
    if planned >= budget:
        break
    take = min(size, budget - planned)
    selected.append((candidate, take))
    planned += take


def warm(entry):
    path, limit = entry
    read = 0
    with open(path, "rb", buffering=0) as handle:
        while read < limit:
            if time.monotonic() > deadline:
                return read
            block = handle.read(min(chunk, limit - read))
            if not block:
                break
            read += len(block)
    return read


started = time.monotonic()
total = 0
with futures.ThreadPoolExecutor(max_workers=workers) as pool:
    for value in pool.map(warm, selected):
        total += value
elapsed = time.monotonic() - started
json.dump(
    {
        "schema": "fs2-serve.nebius.ai/fast-start-warm-page-cache/v1",
        "root": root,
        "files": len(selected),
        "planned_bytes": planned,
        "read_bytes": total,
        "seconds": round(elapsed, 3),
        "complete": total >= planned,
    },
    sys.stdout,
)
sys.stdout.write("\n")
"""


def configure_regional_cache(
    *,
    pod_spec: dict[str, Any],
    pod_metadata: dict[str, Any],
    qualification: RegionalCacheQualification,
    runtime_image: str,
    runtime_container_name: str | None = None,
    compile_cache_volume_name: str = "runtime-cache",
) -> None:
    """Serve from the in-region mirror, retain the compile cache, warm the pages.

    The runtime image must already be the in-region mirror's digest; a foreign
    registry is rejected rather than pulled across regions while claiming a
    regional-cache path.  The compile-cache volume is switched from a discarded
    ``emptyDir`` to the retained claim under an ABI-scoped sub-path, and an init
    container pre-reads the immutable payload so its pages are warm.
    """

    if re.fullmatch(IMAGE_DIGEST_PATTERN, runtime_image) is None:
        raise FastStartMechanismError("regional-cache requires a digest-pinned runtime image")
    registry = runtime_image.split("/", 1)[0]
    if registry != qualification.image_mirror_registry:
        raise FastStartMechanismError("regional-cache requires the runtime image to come from the in-region mirror")

    container = _container(pod_spec, runtime_container_name)
    cache = qualification.compile_cache
    _replace_volume(
        pod_spec,
        {
            "name": compile_cache_volume_name,
            "persistentVolumeClaim": {"claimName": cache.claim_name},
        },
    )
    mounts = _volume_mounts(container)
    retained_mounts = [item for item in mounts if item.get("name") != compile_cache_volume_name]
    retained_mounts.append(
        {
            "name": compile_cache_volume_name,
            "mountPath": cache.mount_path,
            "subPath": cache.sub_path,
        }
    )
    container["volumeMounts"] = retained_mounts
    _set_env(
        container,
        {
            "VLLM_CACHE_ROOT": f"{cache.mount_path.rstrip('/')}/vllm",
            "TRITON_CACHE_DIR": f"{cache.mount_path.rstrip('/')}/triton",
            "TORCHINDUCTOR_CACHE_DIR": f"{cache.mount_path.rstrip('/')}/inductor",
            "FS2_FAST_START_MECHANISM": FastStartMechanism.REGIONAL_CACHE.value,
        },
    )

    warm = qualification.warm_page_cache
    if warm is not None:
        located = payload_mount(pod_spec, container, qualification.payload_claim_name)
        if located is None:
            raise FastStartMechanismError(
                "regional-cache page warming needs the declared retained payload claim mounted into the runtime"
            )
        payload_volume, payload_path = located
        init_containers = pod_spec.setdefault("initContainers", [])
        if not isinstance(init_containers, list):
            raise FastStartMechanismError("rendered Pod initContainers must be a list")
        init_containers[:] = [
            item
            for item in init_containers
            if not (isinstance(item, dict) and item.get("name") == "fs2-warm-page-cache")
        ]
        init_containers.append(
            {
                "name": "fs2-warm-page-cache",
                "image": runtime_image,
                "command": ["python3", "-c", WARM_PAGE_CACHE_SCRIPT],
                "env": [
                    {"name": "FS2_WARM_ROOT", "value": qualification.payload_content_path},
                    {"name": "FS2_WARM_BYTES_LIMIT", "value": str(warm.read_bytes_limit)},
                    {"name": "FS2_WARM_WORKERS", "value": str(warm.workers)},
                    {"name": "FS2_WARM_TIMEOUT_SECONDS", "value": str(warm.timeout_seconds)},
                ],
                "resources": {
                    "requests": {"cpu": "2", "memory": "1Gi"},
                    "limits": {"cpu": str(min(warm.workers, 16)), "memory": "4Gi"},
                },
                "securityContext": {
                    "allowPrivilegeEscalation": False,
                    "capabilities": {"drop": ["ALL"]},
                },
                "volumeMounts": [{"name": payload_volume, "mountPath": payload_path, "readOnly": True}],
            }
        )

    _annotate(
        pod_metadata,
        {
            MECHANISM_ANNOTATION: FastStartMechanism.REGIONAL_CACHE.value,
            MECHANISM_CONFIG_DIGEST_ANNOTATION: qualification.config_digest,
            "fast-start.fs2.nebius/retained-compile-cache-abi": cache.abi,
        },
    )


RESIDENCY_VERIFY_SCRIPT = r"""
import errno
import fcntl
import json
import os
import re
import sys
import time

root = os.environ["FS2_RESIDENCY_RECEIPT_ROOT"]
holder_id = os.environ["FS2_RESIDENCY_HOLDER_ID"]
expected_digest = os.environ["FS2_RESIDENCY_CONFIG_DIGEST"]
expected_payload = os.environ["FS2_RESIDENCY_PAYLOAD_DIGEST"]
expected_bytes = int(os.environ["FS2_RESIDENCY_BYTES"])
node = os.environ["FS2_NODE_NAME"]
path = os.path.join(root, holder_id, node, "receipt.json")
max_age = float(os.environ["FS2_RESIDENCY_MAX_AGE_SECONDS"])
deadline = time.monotonic() + float(os.environ.get("FS2_RESIDENCY_WAIT_SECONDS", "120"))
incarnation_pattern = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")

def incarnation_is_live(value):
    if not isinstance(value, str) or incarnation_pattern.fullmatch(value) is None:
        return False
    lock_path = os.path.join(root, holder_id, node, "incarnations", value + ".lock")
    # The receipt claim is deliberately mounted read-only in serving Pods.  A
    # shared-lock probe only needs read access: it is refused while the holder's
    # process-lifetime exclusive lock is live and succeeds after that holder
    # exits.  Opening O_RDWR here makes every real read-only Kubernetes mount
    # look like a dead holder before flock is even attempted.
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(lock_path, flags)
    except OSError:
        return False
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except OSError as exc:
            return exc.errno in (errno.EACCES, errno.EAGAIN)
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        return False
    finally:
        os.close(descriptor)

while True:
    reason = None
    try:
        with open(path, "rb") as handle:
            receipt = json.loads(handle.read(1 << 20).decode("utf-8"))
    except (OSError, ValueError):
        reason = "receipt_unavailable"
        receipt = {}
    if reason is None:
        if receipt.get("schema") != "fs2-serve.nebius.ai/fast-start-host-memory-residency-receipt/v2":
            reason = "receipt_schema_mismatch"
        elif receipt.get("holder_id") != holder_id:
            reason = "receipt_holder_mismatch"
        elif receipt.get("node_name") != node:
            reason = "receipt_node_mismatch"
        elif receipt.get("config_digest") != expected_digest:
            reason = "receipt_config_mismatch"
        elif receipt.get("payload_digest") != expected_payload:
            reason = "receipt_payload_mismatch"
        elif receipt.get("content_digest_verified") is not True:
            reason = "receipt_content_unverified"
        elif int(receipt.get("resident_bytes", -1)) < expected_bytes:
            reason = "receipt_resident_bytes_short"
        elif time.time() - float(receipt.get("refreshed_at_epoch", 0.0)) > max_age:
            reason = "receipt_stale"
        elif not incarnation_is_live(receipt.get("holder_incarnation")):
            reason = "receipt_holder_incarnation_not_live"
    if reason is None:
        json.dump(
            {
                "schema": "fs2-serve.nebius.ai/fast-start-host-memory-residency-admission/v1",
                "node_name": node,
                "config_digest": expected_digest,
                "resident_bytes": int(receipt["resident_bytes"]),
                "residency_mode": receipt.get("residency_mode"),
                "admitted": True,
            },
            sys.stdout,
        )
        sys.stdout.write("\n")
        sys.exit(0)
    if time.monotonic() > deadline:
        sys.stderr.write("host-memory residency not admitted: %s\n" % reason)
        sys.exit(1)
    time.sleep(2.0)
"""


def configure_host_memory_residency(
    *,
    pod_spec: dict[str, Any],
    pod_metadata: dict[str, Any],
    qualification: HostMemoryResidencyQualification,
    runtime_image: str,
    model_ref: str,
    holder_identity: str,
    runtime_container_name: str | None = None,
) -> None:
    """Require a node-local receipt from the pool's per-node residency holder.

    The holder is a DaemonSet on every eligible node. Avoiding required
    inter-Pod affinity lets a serving Pod trigger scale-up from a zero-node
    pool; its init container then waits until the holder on that exact node has
    published a matching receipt. The reserved bytes are annotated on the Pod
    so the node RAM cost travels with the workload.
    """

    if re.fullmatch(IMAGE_DIGEST_PATTERN, runtime_image) is None:
        raise FastStartMechanismError("host-memory-residency requires a digest-pinned runtime image")
    if qualification.residency_mode == "runtime-sleep-offload":
        raise FastStartMechanismError(
            "runtime-sleep-offload is unavailable until a production sleep/wake controller is installed"
        )
    container = _container(pod_spec, runtime_container_name)
    holder = qualification.holder
    _replace_volume(
        pod_spec,
        {
            "name": "residency-receipt",
            "persistentVolumeClaim": {"claimName": holder.receipt_claim_name, "readOnly": True},
        },
    )
    init_containers = pod_spec.setdefault("initContainers", [])
    if not isinstance(init_containers, list):
        raise FastStartMechanismError("rendered Pod initContainers must be a list")
    init_containers[:] = [
        item
        for item in init_containers
        if not (isinstance(item, dict) and item.get("name") == "fs2-verify-host-memory-residency")
    ]
    init_containers.append(
        {
            "name": "fs2-verify-host-memory-residency",
            "image": runtime_image,
            "command": ["python3", "-c", RESIDENCY_VERIFY_SCRIPT],
            "env": [
                {
                    "name": "FS2_RESIDENCY_RECEIPT_ROOT",
                    "value": holder.receipt_mount_path,
                },
                {"name": "FS2_RESIDENCY_HOLDER_ID", "value": holder_identity},
                {"name": "FS2_RESIDENCY_CONFIG_DIGEST", "value": qualification.config_digest},
                {"name": "FS2_RESIDENCY_PAYLOAD_DIGEST", "value": qualification.payload_digest},
                {"name": "FS2_RESIDENCY_BYTES", "value": str(qualification.payload_bytes)},
                {"name": "FS2_RESIDENCY_MAX_AGE_SECONDS", "value": str(qualification.receipt_max_age_seconds)},
                {"name": "FS2_NODE_NAME", "valueFrom": {"fieldRef": {"fieldPath": "spec.nodeName"}}},
            ],
            "resources": {
                "requests": {"cpu": "100m", "memory": "128Mi"},
                "limits": {"cpu": "1", "memory": "256Mi"},
            },
            "securityContext": {
                "allowPrivilegeEscalation": False,
                "capabilities": {"drop": ["ALL"]},
            },
            "volumeMounts": [
                {
                    "name": "residency-receipt",
                    "mountPath": holder.receipt_mount_path,
                    "readOnly": True,
                }
            ],
        }
    )
    _set_env(
        container,
        {
            "FS2_FAST_START_MECHANISM": FastStartMechanism.HOST_MEMORY_RESIDENCY.value,
            "FS2_FAST_START_RESIDENCY_MODE": qualification.residency_mode,
        },
    )

    _annotate(
        pod_metadata,
        {
            MECHANISM_ANNOTATION: FastStartMechanism.HOST_MEMORY_RESIDENCY.value,
            MECHANISM_CONFIG_DIGEST_ANNOTATION: qualification.config_digest,
            MECHANISM_RESERVED_MEMORY_ANNOTATION: str(qualification.reserved_bytes),
            "fast-start.fs2.nebius/residency-mode": qualification.residency_mode,
        },
    )


_MEMORY_SUFFIXES: dict[str, int] = {
    "": 1,
    "k": 1000,
    "K": 1000,
    "M": 1000**2,
    "G": 1000**3,
    "T": 1000**4,
    "Ki": 1024,
    "Mi": 1024**2,
    "Gi": 1024**3,
    "Ti": 1024**4,
}


def parse_memory_quantity(value: str) -> int:
    """Parse the Kubernetes memory quantities this module renders and reads."""

    match = re.fullmatch(r"(\d+)([EPTGMK]i?|[kmgt])?", value.strip())
    if match is None:
        raise FastStartMechanismError("unsupported Kubernetes memory quantity")
    amount, suffix = match.group(1), match.group(2) or ""
    if suffix not in _MEMORY_SUFFIXES:
        raise FastStartMechanismError("unsupported Kubernetes memory quantity suffix")
    return int(amount) * _MEMORY_SUFFIXES[suffix]


def scheduled_pod_memory_bytes(pod_spec: Mapping[str, Any]) -> int:
    """Return Kubernetes' effective memory request for one rendered Pod."""

    def container_request(container: object) -> int:
        if not isinstance(container, Mapping):
            raise FastStartMechanismError("rendered Pod container must be an object")
        resources = container.get("resources", {})
        if not isinstance(resources, Mapping):
            raise FastStartMechanismError("rendered container resources must be an object")
        requests = resources.get("requests", {})
        if not isinstance(requests, Mapping):
            raise FastStartMechanismError("rendered container resource requests must be an object")
        memory = requests.get("memory")
        if memory is None:
            return 0
        if not isinstance(memory, str):
            raise FastStartMechanismError("rendered container memory request must be a quantity string")
        return parse_memory_quantity(memory)

    regular = sum(container_request(item) for item in pod_spec.get("containers", []))
    init = max((container_request(item) for item in pod_spec.get("initContainers", [])), default=0)
    overhead = pod_spec.get("overhead", {})
    if not isinstance(overhead, Mapping):
        raise FastStartMechanismError("rendered Pod overhead must be an object")
    overhead_memory = overhead.get("memory")
    if overhead_memory is not None and not isinstance(overhead_memory, str):
        raise FastStartMechanismError("rendered Pod memory overhead must be a quantity string")
    return max(regular, init) + (0 if overhead_memory is None else parse_memory_quantity(overhead_memory))


RESIDENCY_AGENT_SCRIPT_NAME = "residency_agent.py"


def residency_agent_script() -> str:
    """Return the packaged residency agent as the holder's ConfigMap payload.

    The agent ships inside this package rather than being supplied per model,
    so onboarding a model needs a declaration and nothing else.
    """

    return (Path(__file__).with_name(RESIDENCY_AGENT_SCRIPT_NAME)).read_text(encoding="utf-8")


def residency_holder_manifests(
    *,
    namespace: str,
    name: str,
    model_ref: str,
    holder_identity: str,
    qualification: HostMemoryResidencyQualification,
    image: str,
    node_selector: Mapping[str, str],
    tolerations: Sequence[Mapping[str, Any]],
    labels: Mapping[str, str],
    annotations: Mapping[str, str],
    pod_security_context: Mapping[str, Any] | None = None,
    owner_references: Sequence[Mapping[str, Any]] = (),
) -> list[dict[str, Any]]:
    """Render the node-scoped host-memory residency holder.

    The holder is the mechanism's explicit price tag: its memory request and
    limit are both the declared ``reservedBytes``, so the node RAM the
    mechanism consumes is scheduled, visible and attributable instead of being
    an incidental page-cache effect that another workload can evict.

    ``pod_security_context`` must be the runtime's own, because the holder has
    to read exactly the bytes the runtime reads.  A retained payload on a shared
    filesystem is owned by the runtime's user, and a holder that ran as a
    different identity would be denied the very files it is supposed to hold.
    """

    if qualification.residency_mode == "runtime-sleep-offload":
        raise FastStartMechanismError("sleep-offload residency is held by the runtime, not by a holder")
    if re.fullmatch(IMAGE_DIGEST_PATTERN, image) is None:
        raise FastStartMechanismError("the residency holder requires a digest-pinned image")
    reserved = str(qualification.reserved_bytes)
    holder_labels = {
        **dict(labels),
        HOST_MEMORY_RESIDENCY_LABEL: model_ref,
        HOST_MEMORY_RESIDENCY_HOLDER_LABEL: holder_identity,
    }
    holder_annotations = {
        **dict(annotations),
        MECHANISM_ANNOTATION: FastStartMechanism.HOST_MEMORY_RESIDENCY.value,
        MECHANISM_CONFIG_DIGEST_ANNOTATION: qualification.config_digest,
        MECHANISM_RESERVED_MEMORY_ANNOTATION: reserved,
        "fast-start.fs2.nebius/residency-mode": qualification.residency_mode,
    }
    config_map: dict[str, Any] = {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {
            "name": f"{name}-agent",
            "namespace": namespace,
            "labels": dict(holder_labels),
            "annotations": dict(holder_annotations),
        },
        "data": {RESIDENCY_AGENT_SCRIPT_NAME: residency_agent_script()},
    }
    locked = qualification.residency_mode == "locked-payload-residency"
    security_context: dict[str, Any] = {
        "allowPrivilegeEscalation": False,
        "capabilities": {"drop": ["ALL"], "add": ["IPC_LOCK"]} if locked else {"drop": ["ALL"]},
    }
    daemon_set: dict[str, Any] = {
        "apiVersion": "apps/v1",
        "kind": "DaemonSet",
        "metadata": {
            "name": name,
            "namespace": namespace,
            "labels": dict(holder_labels),
            "annotations": dict(holder_annotations),
        },
        "spec": {
            "selector": {"matchLabels": {HOST_MEMORY_RESIDENCY_HOLDER_LABEL: holder_identity}},
            "updateStrategy": {"type": "RollingUpdate"},
            "template": {
                "metadata": {
                    "labels": dict(holder_labels),
                    "annotations": dict(holder_annotations),
                },
                "spec": {
                    "nodeSelector": dict(node_selector),
                    "tolerations": [dict(item) for item in tolerations],
                    **({"securityContext": dict(pod_security_context)} if pod_security_context else {}),
                    "containers": [
                        {
                            "name": "residency-agent",
                            "image": image,
                            "command": ["python3", f"/agent/{RESIDENCY_AGENT_SCRIPT_NAME}"],
                            "env": [
                                {"name": "FS2_RESIDENCY_MODEL_REF", "value": model_ref},
                                {"name": "FS2_RESIDENCY_HOLDER_ID", "value": holder_identity},
                                {"name": "FS2_RESIDENCY_MODE", "value": qualification.residency_mode},
                                {
                                    "name": "FS2_RESIDENCY_PAYLOAD_ROOT",
                                    "value": qualification.payload_content_path,
                                },
                                {"name": "FS2_RESIDENCY_PAYLOAD_DIGEST", "value": qualification.payload_digest},
                                {"name": "FS2_RESIDENCY_PAYLOAD_BYTES", "value": str(qualification.payload_bytes)},
                                {"name": "FS2_RESIDENCY_RESERVED_BYTES", "value": reserved},
                                {"name": "FS2_RESIDENCY_CONFIG_DIGEST", "value": qualification.config_digest},
                                {
                                    "name": "FS2_RESIDENCY_RECEIPT_ROOT",
                                    "value": qualification.holder.receipt_mount_path,
                                },
                                {
                                    "name": "FS2_RESIDENCY_REFRESH_SECONDS",
                                    "value": str(max(5, qualification.receipt_max_age_seconds // 3)),
                                },
                                {"name": "FS2_NODE_NAME", "valueFrom": {"fieldRef": {"fieldPath": "spec.nodeName"}}},
                                {
                                    "name": "FS2_RESIDENCY_HOLDER_INCARNATION",
                                    "valueFrom": {"fieldRef": {"fieldPath": "metadata.uid"}},
                                },
                            ],
                            "resources": {
                                "requests": {"cpu": "2", "memory": reserved},
                                "limits": {"cpu": "8", "memory": reserved},
                            },
                            "securityContext": security_context,
                            "readinessProbe": {
                                "exec": {
                                    "command": [
                                        "python3",
                                        f"/agent/{RESIDENCY_AGENT_SCRIPT_NAME}",
                                        "--check",
                                    ]
                                },
                                "initialDelaySeconds": 5,
                                "periodSeconds": 10,
                                "timeoutSeconds": 5,
                                "failureThreshold": 3,
                            },
                            "volumeMounts": [
                                {"name": "agent", "mountPath": "/agent", "readOnly": True},
                                {
                                    "name": "payload",
                                    "mountPath": _payload_mount_root(qualification.payload_content_path),
                                    "readOnly": True,
                                },
                                {"name": "receipt", "mountPath": qualification.holder.receipt_mount_path},
                            ],
                        }
                    ],
                    "volumes": [
                        {"name": "agent", "configMap": {"name": f"{name}-agent", "defaultMode": 292}},
                        {
                            "name": "payload",
                            "persistentVolumeClaim": {
                                "claimName": qualification.payload_claim_name,
                                "readOnly": True,
                            },
                        },
                        {
                            "name": "receipt",
                            "persistentVolumeClaim": {"claimName": qualification.holder.receipt_claim_name},
                        },
                    ],
                },
            },
        },
    }
    if owner_references:
        references = [dict(item) for item in owner_references]
        config_map["metadata"]["ownerReferences"] = references
        daemon_set["metadata"]["ownerReferences"] = [dict(item) for item in references]
    return [config_map, daemon_set]


def _payload_mount_root(content_path: str) -> str:
    """Return the mount root that must expose ``content_path``.

    The retained payload claim is mounted at its top-level directory so the
    content-addressed path inside it stays byte-identical to the conventional
    render's path.  An identical path keeps the runtime's argv, and therefore
    its runtime-contract digest, unchanged between mechanisms.
    """

    parts = [part for part in content_path.split("/") if part]
    if not parts:
        raise FastStartMechanismError("the retained payload content path is empty")
    return f"/{parts[0]}"


def configure_gpu_resident(
    *,
    pod_spec: dict[str, Any],
    pod_metadata: dict[str, Any],
    qualification: GpuResidentQualification,
    configured_hot_replicas: int,
    role: Literal["standby", "serving"] = "standby",
    runtime_container_name: str | None = None,
) -> None:
    """Park a warm engine in GPU memory and promote it instead of loading it.

    A ``standby`` replica loads the engine, keeps the weights in GPU memory, and
    carries a readiness gate that holds it out of the Service until the promoter
    sets the gate's condition.  Activation is then one readiness period rather
    than a model load.  The Pod records the standby count, the reserved
    accelerators and the hot floor the mechanism depends on.
    """

    if configured_hot_replicas < qualification.minimum_hot_replicas:
        raise FastStartMechanismError("gpu-resident needs the deployment's hot floor to cover its declared minimum")
    container = _container(pod_spec, runtime_container_name)
    _set_env(
        container,
        {
            "FS2_FAST_START_MECHANISM": FastStartMechanism.GPU_RESIDENT.value,
            "FS2_FAST_START_RESIDENCY_MODE": qualification.residency_mode,
        },
    )
    if role == "standby":
        gates = pod_spec.setdefault("readinessGates", [])
        if not isinstance(gates, list):
            raise FastStartMechanismError("rendered Pod readinessGates must be a list")
        gate = {"conditionType": GPU_RESIDENT_READINESS_GATE}
        if gate not in gates:
            gates.append(gate)
        readiness = container.get("readinessProbe")
        if isinstance(readiness, dict):
            probe = dict(readiness)
            probe["periodSeconds"] = qualification.promotion_probe_period_seconds
            container["readinessProbe"] = probe
    _label(pod_metadata, {GPU_RESIDENT_ROLE_LABEL: role})
    _annotate(
        pod_metadata,
        {
            MECHANISM_ANNOTATION: FastStartMechanism.GPU_RESIDENT.value,
            MECHANISM_CONFIG_DIGEST_ANNOTATION: qualification.config_digest,
            MECHANISM_STANDBY_ANNOTATION: str(qualification.standby_replicas),
            MECHANISM_HOT_FLOOR_ANNOTATION: str(qualification.minimum_hot_replicas),
            "fast-start.fs2.nebius/gpu-resident-reserved-accelerators": str(qualification.reserved_accelerators),
            "fast-start.fs2.nebius/residency-mode": qualification.residency_mode,
        },
    )


class FastStartCacheMechanismPool(KubernetesModel):
    """Per-pool projection of one mechanism.

    ``mechanismConfigDigest`` is the value a benchmark receipt must carry for
    this pool before its cohort can qualify a level.  Publishing it lets an
    operator see why a receipt was retained instead of counted.
    """

    availability: Literal["Available", "Unavailable"]
    reason: str = Field(min_length=1, max_length=64, pattern=REASON_PATTERN)
    evidence_selector: dict[str, str] = Field(default_factory=dict, max_length=8)
    mechanism_config_digest: str | None = Field(default=None, pattern=SHA256_DIGEST_PATTERN)


class FastStartCacheMechanismStatus(KubernetesModel):
    """Operator detail for one cold-start mechanism, never level proof.

    ``state`` distinguishes a mechanism that is available but not selected
    (``Available``), one that is declared and rendered but whose render has not
    converged (``Pending``), one that is live (``Configured``), and one the
    pools cannot provide at all (``Unavailable``).

    There is deliberately no level, percentile or sample count here.  The level
    lives in the surrounding :class:`~fs2_serve.fast_start.FastStartStatus`,
    where it comes only from compatible benchmark evidence, and it stays ``Off``
    while a ``Configured`` mechanism has no populated cohort.
    """

    state: Literal["Available", "Configured", "Pending", "Unavailable", "Undeclared"]
    selected: bool
    availability: Literal["Available", "Unavailable"]
    reason: str = Field(min_length=1, max_length=64, pattern=REASON_PATTERN)
    config_digest: str | None = Field(default=None, pattern=SHA256_DIGEST_PATTERN)
    residency_mode: str | None = Field(default=None, min_length=1, max_length=64)
    pool_refs: list[str] = Field(min_length=1, max_length=32)
    pools: dict[str, FastStartCacheMechanismPool] = Field(min_length=1, max_length=32)
    retained_payload_bytes: int | None = Field(default=None, ge=0)
    retained_compile_cache_abi: str | None = Field(default=None, min_length=1, max_length=128)
    reserved_host_memory_bytes: int | None = Field(default=None, ge=0)
    host_memory_reservation_scope: Literal["per-node", "per-replica"] | None = None
    maximum_reserved_host_memory_bytes: int | None = Field(default=None, ge=0)
    reserved_host_memory_fraction: float | None = Field(default=None, ge=0, le=1)
    standby_replicas: int | None = Field(default=None, ge=0, le=64)
    reserved_accelerators: int | None = Field(default=None, ge=0, le=4096)
    minimum_hot_replicas: int | None = Field(default=None, ge=0, le=10000)
    configured_hot_replicas: int | None = Field(default=None, ge=0, le=10000)
    telemetry_state: Literal["Unavailable"] = "Unavailable"

    @model_validator(mode="after")
    def consistent_projection(self) -> FastStartCacheMechanismStatus:
        if set(self.pools) != set(self.pool_refs):
            raise ValueError("mechanism pool projection must cover exactly the declared pools")
        if self.state in ("Configured", "Pending") and self.config_digest is None:
            raise ValueError("a declared mechanism must publish its reviewed configuration digest")
        if self.state == "Undeclared" and self.config_digest is not None:
            raise ValueError("an undeclared mechanism cannot publish a configuration digest")
        if self.selected and self.state not in ("Available", "Configured", "Pending"):
            raise ValueError("only an available mechanism can be the selected one")
        if (self.availability == "Unavailable") != (self.state == "Unavailable"):
            raise ValueError("an unavailable mechanism must be reported in the Unavailable state")
        return self


def project_cache_mechanisms(
    *,
    selected: FastStartMechanism | None,
    declarations: Mapping[FastStartMechanism, Any],
    pools: Mapping[str, Mapping[str, str]],
    pool_allocatable_memory_bytes: Mapping[str, int | None],
    pool_max_nodes: Mapping[str, int] | None = None,
    host_residency_pool_refs: set[str] | None = None,
    host_residency_ready: bool | None = None,
    storage_contract_digests: Mapping[str, str],
    converged: bool,
    configured_hot_replicas: int | None,
    configured_max_replicas: int | None = None,
    mechanism_config_digest: Any,
) -> dict[str, FastStartCacheMechanismStatus]:
    """Project every known cold-start mechanism for one deployment.

    Every mechanism the platform knows about appears, including the two that
    this cluster's H100 pool cannot provide, so ``node-local-restore`` is
    reported ``Unavailable`` with the pool selector that proves it rather than
    being silently missing or quietly attempted.

    ``mechanism_config_digest`` is injected to keep this module independent of
    the identity module's import graph.
    """

    if not pools:
        return {}
    projected: dict[str, FastStartCacheMechanismStatus] = {}
    availability_by_pool = {
        pool_ref: {item.mechanism: item for item in assess_pool_mechanisms(pool_id=pool_ref, node_selector=selector)}
        for pool_ref, selector in pools.items()
    }
    for mechanism in FastStartMechanism:
        if mechanism is FastStartMechanism.MODELEXPRESS:
            # ModelExpress has its own reviewed status projection.
            continue
        declaration = declarations.get(mechanism)
        pool_projection: dict[str, FastStartCacheMechanismPool] = {}
        for pool_ref in sorted(pools):
            availability = availability_by_pool[pool_ref][mechanism.value]
            availability_state = availability.state
            availability_reason = availability.reason
            if declaration is not None and pool_ref not in declaration.pool_refs:
                availability_state = "Unavailable"
                availability_reason = "NoReviewedDeclarationForPool"
            elif isinstance(declaration, HostMemoryResidencyQualification):
                allocatable_memory = pool_allocatable_memory_bytes.get(pool_ref)
                if declaration.residency_mode == "runtime-sleep-offload":
                    availability_state = "Unavailable"
                    availability_reason = "SleepWakeControllerNotInstalled"
                elif allocatable_memory is None:
                    availability_state = "Unavailable"
                    availability_reason = "PoolMemoryCapacityUnavailable"
                elif declaration.reserved_bytes > allocatable_memory:
                    availability_state = "Unavailable"
                    availability_reason = "HostMemoryReservationExceedsPool"
            storage_digest = storage_contract_digests.get(pool_ref)
            expected: str | None = None
            if storage_digest is not None and availability_state == "Available":
                expected = mechanism_config_digest(
                    mechanism=mechanism.value,
                    storage_contract_digest=storage_digest,
                    declaration_digest=None if declaration is None else declaration.config_digest,
                )
            pool_projection[pool_ref] = FastStartCacheMechanismPool(
                availability=availability_state,
                reason=availability_reason,
                evidence_selector=availability.evidence_selector,
                mechanism_config_digest=expected,
            )
        unavailable = [item for item in pool_projection.values() if item.availability == "Unavailable"]
        is_selected = selected is mechanism
        if unavailable:
            state: Literal["Available", "Configured", "Pending", "Unavailable", "Undeclared"] = "Unavailable"
            reason = unavailable[0].reason
        elif mechanism is FastStartMechanism.CONVENTIONAL:
            state = "Available"
            reason = "ConventionalLoaderSelected" if is_selected else "ConventionalLoaderAvailable"
        elif declaration is None:
            state = "Undeclared"
            reason = "NoReviewedDeclaration"
        elif not is_selected:
            state = "Available"
            reason = "ReviewedDeclarationAvailable"
        elif mechanism is FastStartMechanism.HOST_MEMORY_RESIDENCY and host_residency_ready is not True:
            state = "Pending"
            reason = "ResidencyHoldersNotReady"
        elif converged:
            state = "Configured"
            reason = "MechanismRenderConverged"
        else:
            state = "Pending"
            reason = "MechanismRenderPending"
        host_memory_scope: Literal["per-node", "per-replica"] | None = None
        maximum_reserved_host_memory_bytes: int | None = None
        if isinstance(declaration, HostMemoryResidencyQualification):
            host_memory_scope = "per-replica" if declaration.residency_mode == "runtime-sleep-offload" else "per-node"
            if not is_selected:
                maximum_reserved_host_memory_bytes = 0
            elif host_memory_scope == "per-replica" and configured_max_replicas is not None:
                maximum_reserved_host_memory_bytes = declaration.reserved_bytes * configured_max_replicas
            elif host_memory_scope == "per-node" and pool_max_nodes is not None:
                reservation_pools = set(pools) if host_residency_pool_refs is None else host_residency_pool_refs
                maximum_reserved_host_memory_bytes = declaration.reserved_bytes * sum(
                    pool_max_nodes.get(pool_ref, 0) for pool_ref in reservation_pools
                )
        single_pool_memory = pool_allocatable_memory_bytes.get(next(iter(pools))) if len(pools) == 1 else None
        reserved_host_memory_fraction = (
            round(declaration.reserved_bytes / single_pool_memory, 6)
            if isinstance(declaration, HostMemoryResidencyQualification)
            and single_pool_memory is not None
            and single_pool_memory >= declaration.reserved_bytes
            else None
        )
        projected[mechanism.value] = FastStartCacheMechanismStatus(
            state=state,
            selected=is_selected and state in ("Available", "Configured", "Pending"),
            availability="Unavailable" if unavailable else "Available",
            reason=reason,
            config_digest=None if declaration is None else declaration.config_digest,
            residency_mode=getattr(declaration, "residency_mode", None),
            pool_refs=sorted(pools),
            pools=pool_projection,
            retained_payload_bytes=getattr(declaration, "payload_bytes", None),
            retained_compile_cache_abi=(
                declaration.compile_cache.abi if isinstance(declaration, RegionalCacheQualification) else None
            ),
            reserved_host_memory_bytes=(
                declaration.reserved_bytes if isinstance(declaration, HostMemoryResidencyQualification) else None
            ),
            host_memory_reservation_scope=host_memory_scope,
            maximum_reserved_host_memory_bytes=maximum_reserved_host_memory_bytes,
            reserved_host_memory_fraction=reserved_host_memory_fraction,
            standby_replicas=(
                declaration.standby_replicas if isinstance(declaration, GpuResidentQualification) else None
            ),
            reserved_accelerators=(
                declaration.reserved_accelerators if isinstance(declaration, GpuResidentQualification) else None
            ),
            minimum_hot_replicas=(
                declaration.minimum_hot_replicas if isinstance(declaration, GpuResidentQualification) else None
            ),
            configured_hot_replicas=(
                configured_hot_replicas if isinstance(declaration, GpuResidentQualification) else None
            ),
        )
    return projected
