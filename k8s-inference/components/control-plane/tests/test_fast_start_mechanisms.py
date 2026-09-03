"""Cold-start mechanism vocabulary and per-pool availability."""

from __future__ import annotations

import pytest

from fs2_serve.fast_start_mechanisms import (
    SELECTABLE_MECHANISMS,
    FastStartMechanism,
    FastStartMechanismError,
    assess_pool_mechanisms,
    unavailable_mechanisms,
)

# The exact scheduling selector Terraform renders for the retained H100 pool,
# copied from the live ModelDeployment render on cluster k8s-inference-h100.
H100_RESERVED_SELECTOR = {
    "accelerator.fs2.nebius/class": "nvidia-h100-sxm5-80gb",
    "accelerator.fs2.nebius/pool-id": "h100-reserved-8x",
    "capacity.fs2.nebius/gpu-count": "8",
    "capacity.fs2.nebius/source": "capacity-block",
    "capacity.fs2.nebius/type": "regular",
    "local-nvme.fs2.nebius/eligible": "false",
    "snapshot.fs2.nebius/eligible": "false",
    "topology.fs2.nebius/scope": "standalone",
    "workload.fs2.nebius/gpu": "true",
}
NVME_POOL_SELECTOR = {
    **H100_RESERVED_SELECTOR,
    "accelerator.fs2.nebius/pool-id": "hypothetical-nvme-8x",
    "local-nvme.fs2.nebius/eligible": "true",
    "snapshot.fs2.nebius/eligible": "true",
}


def _by_name(pool_id: str, selector: dict[str, str]) -> dict[str, tuple[str, str]]:
    return {
        item.mechanism: (item.state, item.reason)
        for item in assess_pool_mechanisms(pool_id=pool_id, node_selector=selector)
    }


def test_node_local_restore_is_reported_unavailable_on_the_h100_pool() -> None:
    """The H100 pool has no local NVMe, so the path is reported, not attempted."""

    assessed = _by_name("h100-reserved-8x", H100_RESERVED_SELECTOR)
    assert assessed["node-local-restore"] == ("Unavailable", "NoNodeLocalNvme")
    assert assessed["shared-restore"] == ("Unavailable", "NoQualifiedSnapshotCapability")

    blocked = unavailable_mechanisms(pool_id="h100-reserved-8x", node_selector=H100_RESERVED_SELECTOR)
    assert set(blocked) == {"node-local-restore", "shared-restore"}
    # The report carries the exact selector the scheduler uses, so the claim is
    # checkable against the live pool rather than a separate capability list.
    assert blocked["node-local-restore"].evidence_selector == {"local-nvme.fs2.nebius/eligible": "false"}
    assert blocked["shared-restore"].evidence_selector == {"snapshot.fs2.nebius/eligible": "false"}


def test_the_three_implemented_mechanisms_are_available_on_the_h100_pool() -> None:
    assessed = _by_name("h100-reserved-8x", H100_RESERVED_SELECTOR)
    assert assessed["conventional"][0] == "Available"
    assert assessed["regional-cache"][0] == "Available"
    assert assessed["host-memory-residency"][0] == "Available"
    assert assessed["gpu-resident"][0] == "Available"


def test_a_pool_with_local_nvme_unlocks_the_restore_paths() -> None:
    assessed = _by_name("hypothetical-nvme-8x", NVME_POOL_SELECTOR)
    assert assessed["node-local-restore"] == ("Available", "NodeLocalNvmePresent")
    assert assessed["shared-restore"] == ("Available", "SnapshotCapabilityPresent")
    assert not unavailable_mechanisms(pool_id="hypothetical-nvme-8x", node_selector=NVME_POOL_SELECTOR)


def test_a_pool_that_does_not_state_a_capability_is_treated_as_lacking_it() -> None:
    selector = {key: value for key, value in H100_RESERVED_SELECTOR.items() if "local-nvme" not in key}
    assessed = _by_name("h100-reserved-8x", selector)
    assert assessed["node-local-restore"] == ("Unavailable", "NoNodeLocalNvme")
    blocked = unavailable_mechanisms(pool_id="h100-reserved-8x", node_selector=selector)
    assert blocked["node-local-restore"].evidence_selector == {}


def test_every_assessed_mechanism_is_a_known_name_and_none_is_a_level() -> None:
    assessed = assess_pool_mechanisms(pool_id="h100-reserved-8x", node_selector=H100_RESERVED_SELECTOR)
    assert [item.mechanism for item in assessed] == sorted(item.mechanism for item in assessed)
    known = {item.value for item in FastStartMechanism}
    assert {item.mechanism for item in assessed} == known - {FastStartMechanism.MODELEXPRESS.value}
    # No mechanism name may collide with a customer level; levels are Off..L4.
    assert not {item.mechanism for item in assessed} & {"Off", "L1", "L2", "L3", "L4", "L5", "L6", "Hot"}


def test_only_the_implemented_mechanisms_are_selectable() -> None:
    assert SELECTABLE_MECHANISMS == (
        FastStartMechanism.CONVENTIONAL,
        FastStartMechanism.REGIONAL_CACHE,
        FastStartMechanism.HOST_MEMORY_RESIDENCY,
        FastStartMechanism.GPU_RESIDENT,
    )
    assert FastStartMechanism.NODE_LOCAL_RESTORE not in SELECTABLE_MECHANISMS
    assert FastStartMechanism.SHARED_RESTORE not in SELECTABLE_MECHANISMS


def test_an_unnamed_pool_is_rejected() -> None:
    with pytest.raises(FastStartMechanismError):
        assess_pool_mechanisms(pool_id="", node_selector=H100_RESERVED_SELECTOR)
