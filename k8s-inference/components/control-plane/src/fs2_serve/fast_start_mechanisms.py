"""Cold-start optimisation mechanisms behind the customer-facing fast-start levels.

``fast_start.py`` owns what a level *means* and refuses to grant one without
20 comparable failure-free samples at p95 for the exact deployment tuple.  This
module owns the other half: naming the mechanisms that can make a cold start
faster, and reporting which of them a given accelerator pool can actually
provide.

Nothing here can raise a level.  A mechanism is operator detail; the level
comes from evidence alone.

This slice carries the vocabulary, the availability rule, and the reviewed
per-model declaration for each implemented mechanism.  The render adapters that
configure a Pod from a declaration follow in their own change.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from enum import StrEnum
from typing import Any, Literal

from pydantic import Field, model_validator

from .models import KubernetesModel

SHA256_DIGEST_PATTERN = r"^sha256:[a-f0-9]{64}$"
DNS_SUBDOMAIN_PATTERN = r"^[a-z0-9](?:[a-z0-9-]{0,251}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,251}[a-z0-9])?)*$"
CONTENT_PATH_PATTERN = r"^/(?:[A-Za-z0-9._-]+/?)+$"
MECHANISM_NAME_PATTERN = r"^[a-z][a-z0-9-]*$"
REASON_PATTERN = r"^[A-Za-z][A-Za-z0-9]*$"


# Pool capability is read from the exact node selector Terraform renders for the
# pool, so an unavailable mechanism is proved by the same value the scheduler
# uses rather than by a separate hand-maintained capability list.
LOCAL_NVME_ELIGIBLE_LABEL = "local-nvme.fs2.nebius/eligible"
SNAPSHOT_ELIGIBLE_LABEL = "snapshot.fs2.nebius/eligible"

# A residency holder trades host RAM for start latency.  Refuse a declaration
# that would quietly consume a large share of a shared node.
MAX_RESERVED_MEMORY_FRACTION = 0.25
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
    FastStartMechanism.GPU_RESIDENT,
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

    The three implemented mechanisms need retained regional storage rather than
    a node capability, which is a per-model storage contract; pool-level
    availability therefore says only that the pool can host them.
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
            state="Available",
            reason="StandbyAcceleratorPromotionSupported",
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
        if self.reserved_bytes > int(self.node_allocatable_bytes * MAX_RESERVED_MEMORY_FRACTION):
            raise ValueError("a residency reservation cannot exceed a quarter of the node's allocatable memory")
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
