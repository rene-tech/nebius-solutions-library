"""Cold-start optimisation mechanisms behind the customer-facing fast-start levels.

``fast_start.py`` owns what a level *means* and refuses to grant one without
20 comparable failure-free samples at p95 for the exact deployment tuple.  This
module owns the other half: naming the mechanisms that can make a cold start
faster, and reporting which of them a given accelerator pool can actually
provide.

Nothing here can raise a level.  A mechanism is operator detail; the level
comes from evidence alone.

This first slice is the vocabulary and the availability rule.  The mechanism
declarations and the render adapters that configure a Pod for each mechanism
follow in their own changes.
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from typing import Literal

from pydantic import Field

from .models import KubernetesModel

SHA256_DIGEST_PATTERN = r"^sha256:[a-f0-9]{64}$"
MECHANISM_NAME_PATTERN = r"^[a-z][a-z0-9-]*$"
REASON_PATTERN = r"^[A-Za-z][A-Za-z0-9]*$"


# Pool capability is read from the exact node selector Terraform renders for the
# pool, so an unavailable mechanism is proved by the same value the scheduler
# uses rather than by a separate hand-maintained capability list.
LOCAL_NVME_ELIGIBLE_LABEL = "local-nvme.fs2.nebius/eligible"
SNAPSHOT_ELIGIBLE_LABEL = "snapshot.fs2.nebius/eligible"


class FastStartMechanismError(ValueError):
    """A mechanism declaration or render input is not usable."""


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
