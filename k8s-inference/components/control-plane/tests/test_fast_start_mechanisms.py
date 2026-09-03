"""Cold-start mechanism vocabulary and per-pool availability."""

from __future__ import annotations

import copy
from typing import Any

import pytest

from fs2_serve.fast_start_identity import mechanism_config_digest
from fs2_serve.fast_start_mechanisms import (
    SELECTABLE_MECHANISMS,
    FastStartMechanism,
    FastStartMechanismError,
    GpuResidentQualification,
    HostMemoryResidencyQualification,
    RegionalCacheQualification,
    ResidencyHolder,
    RetainedCompileCache,
    WarmPageCacheReadAhead,
    assess_pool_mechanisms,
    configure_regional_cache,
    unavailable_mechanisms,
)

QWEN_IMAGE = "cr.eu-north1.nebius.cloud/e00akg9ndpx77eaexh/fs2-models/vllm-openai@sha256:" + "22" * 32

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
PLACEHOLDER_DIGEST = "sha256:" + "0" * 64
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


def _regional_cache(**overrides: object) -> RegionalCacheQualification:
    compile_cache = RetainedCompileCache(
        claim_name="fsm-compile-cache-rwx",
        sub_path="qwen3-8b/driver-580.159.04-sm90",
        abi="driver-580.159.04-sm90",
        mount_path="/runtime-cache",
        size_limit_bytes=16 * 1024**3,
    )
    fields: dict[str, object] = {
        "image_mirror_registry": "cr.eu-north1.nebius.cloud",
        "payload_claim_name": "qwen3-8b-cache-rwx-7af24455",
        "payload_content_path": "/models/qwen3-8b/payload",
        "payload_bytes": 16397461266,
        "compile_cache": compile_cache,
        "warm_page_cache": WarmPageCacheReadAhead(
            workers=16,
            read_bytes_limit=16397461266,
            timeout_seconds=600,
        ),
        "pool_refs": ["h100-reserved-8x"],
    }
    fields.update(overrides)
    digest = RegionalCacheQualification.model_construct(
        config_digest=PLACEHOLDER_DIGEST, **fields
    ).expected_config_digest()
    return RegionalCacheQualification(config_digest=digest, **fields)


def _host_memory(mode: str = "locked-payload-residency", **overrides: object) -> HostMemoryResidencyQualification:
    holder = ResidencyHolder(
        name="fsm-hostmem-qwen3-8b",
        namespace="fs2-models",
        receipt_claim_name="fsm-residency-receipt-rwx",
        receipt_mount_path="/residency",
    )
    fields: dict[str, object] = {
        "residency_mode": mode,
        "payload_claim_name": "qwen3-8b-cache-rwx-7af24455",
        "payload_content_path": "/models/qwen3-8b/payload",
        "payload_digest": "sha256:" + "5b" * 32,
        "payload_bytes": 16397461266,
        "reserved_bytes": 19327352832,
        "node_allocatable_bytes": 1648745732096,
        "holder": holder,
        "receipt_max_age_seconds": 180,
        "pool_refs": ["h100-reserved-8x"],
    }
    fields.update(overrides)
    digest = HostMemoryResidencyQualification.model_construct(
        config_digest=PLACEHOLDER_DIGEST, **fields
    ).expected_config_digest()
    return HostMemoryResidencyQualification(config_digest=digest, **fields)


def _gpu_resident(**overrides: object) -> GpuResidentQualification:
    fields: dict[str, object] = {
        "residency_mode": "standby-engine",
        "standby_replicas": 1,
        "accelerators_per_standby_replica": 1,
        "minimum_hot_replicas": 1,
        "promotion_probe_period_seconds": 1,
        "pool_refs": ["h100-reserved-8x"],
    }
    fields.update(overrides)
    digest = GpuResidentQualification.model_construct(
        config_digest=PLACEHOLDER_DIGEST, **fields
    ).expected_config_digest()
    return GpuResidentQualification(config_digest=digest, **fields)


def test_a_declaration_verifies_its_own_configuration_digest() -> None:
    declaration = _regional_cache()
    assert declaration.config_digest == declaration.expected_config_digest()
    with pytest.raises(ValueError, match="configDigest does not match"):
        RegionalCacheQualification(
            **{
                **declaration.model_dump(exclude={"config_digest"}),
                "config_digest": PLACEHOLDER_DIGEST,
            }
        )


def test_retuning_a_mechanism_changes_its_declaration_digest() -> None:
    """A retuned mechanism must not inherit the old cohort's percentile."""

    baseline = _regional_cache()
    retuned = _regional_cache(payload_bytes=baseline.payload_bytes + 1)
    assert retuned.config_digest != baseline.config_digest

    storage = "sha256:" + "ab" * 32
    plain = mechanism_config_digest(mechanism="regional-cache", storage_contract_digest=storage)
    declared = mechanism_config_digest(
        mechanism="regional-cache",
        storage_contract_digest=storage,
        declaration_digest=baseline.config_digest,
    )
    assert plain != declared
    # An absent declaration keeps the historical identity byte for byte, so
    # retained conventional receipts stay compatible.
    assert plain == mechanism_config_digest(
        mechanism="regional-cache",
        storage_contract_digest=storage,
        declaration_digest=None,
    )


def test_a_compile_cache_is_scoped_to_its_abi() -> None:
    with pytest.raises(ValueError, match="must contain its compile-cache ABI"):
        RetainedCompileCache(
            claim_name="fsm-compile-cache-rwx",
            sub_path="qwen3-8b/unrelated",
            abi="driver-580.159.04-sm90",
            mount_path="/runtime-cache",
            size_limit_bytes=1 << 30,
        )
    # Parent traversal cannot even be spelled: the field pattern rejects it
    # before the ABI rule runs, so a sub-path can never escape the claim.
    with pytest.raises(ValueError, match="string_pattern_mismatch|should match pattern"):
        RetainedCompileCache(
            claim_name="fsm-compile-cache-rwx",
            sub_path="../driver-580.159.04-sm90",
            abi="driver-580.159.04-sm90",
            mount_path="/runtime-cache",
            size_limit_bytes=1 << 30,
        )


def test_host_memory_residency_states_its_ram_price_explicitly() -> None:
    declaration = _host_memory()
    assert declaration.reserved_bytes >= declaration.payload_bytes
    assert declaration.reserved_fraction_of_node == pytest.approx(19327352832 / 1648745732096)

    with pytest.raises(ValueError, match="payload plus holder headroom"):
        _host_memory(reserved_bytes=16397461266)
    with pytest.raises(ValueError, match="quarter of the node"):
        _host_memory(reserved_bytes=1648745732096 // 2)


def test_sleep_offload_residency_is_held_by_the_runtime() -> None:
    declaration = _host_memory(mode="runtime-sleep-offload")
    assert declaration.residency_mode == "runtime-sleep-offload"
    # The engine holds its own offloaded weights, so no holder headroom applies,
    # but the reservation must still cover them.
    with pytest.raises(ValueError, match="cover the offloaded weights"):
        _host_memory(mode="runtime-sleep-offload", reserved_bytes=1024)


def test_gpu_resident_states_its_accelerator_price_and_hot_floor() -> None:
    declaration = _gpu_resident(standby_replicas=2, accelerators_per_standby_replica=2)
    assert declaration.reserved_accelerators == 4
    assert declaration.minimum_hot_replicas == 1
    with pytest.raises(ValueError, match="at least one hot replica"):
        _gpu_resident(residency_mode="warm-engine-hot-floor", minimum_hot_replicas=0)


CONVENTIONAL_POD_SPEC: dict[str, object] = {
    "containers": [
        {
            "name": "vllm",
            "image": QWEN_IMAGE,
            "env": [
                {"name": "VLLM_CACHE_ROOT", "value": "/runtime-cache/vllm"},
                {"name": "TRITON_CACHE_DIR", "value": "/runtime-cache/triton"},
            ],
            "volumeMounts": [
                {"name": "model", "mountPath": "/models", "readOnly": True},
                {"name": "runtime-cache", "mountPath": "/runtime-cache"},
            ],
        }
    ],
    "volumes": [
        {"name": "model", "persistentVolumeClaim": {"claimName": "qwen3-8b-cache-rwx-7af24455"}},
        # The live render discards the JIT cache with the Pod. That discarded
        # cache is exactly what regional-cache retains.
        {"name": "runtime-cache", "emptyDir": {"sizeLimit": "16Gi"}},
    ],
}


def _conventional() -> tuple[dict[str, Any], dict[str, Any]]:
    return copy.deepcopy(CONVENTIONAL_POD_SPEC), {"labels": {}, "annotations": {}}


def test_regional_cache_retains_the_compile_cache_the_conventional_render_discards() -> None:
    pod_spec, metadata = _conventional()
    configure_regional_cache(
        pod_spec=pod_spec,
        pod_metadata=metadata,
        qualification=_regional_cache(),
        runtime_image=QWEN_IMAGE,
        runtime_container_name="vllm",
    )
    volumes = {item["name"]: item for item in pod_spec["volumes"]}
    assert "emptyDir" not in volumes["runtime-cache"]
    assert volumes["runtime-cache"]["persistentVolumeClaim"] == {"claimName": "fsm-compile-cache-rwx"}

    container = pod_spec["containers"][0]
    mount = next(item for item in container["volumeMounts"] if item["name"] == "runtime-cache")
    # The ABI is in the subPath, so a driver or architecture change cannot read
    # a cache built by an incompatible stack.
    assert mount["subPath"] == "qwen3-8b/driver-580.159.04-sm90"
    environment = {item["name"]: item["value"] for item in container["env"]}
    assert environment["VLLM_CACHE_ROOT"] == "/runtime-cache/vllm"
    assert environment["TORCHINDUCTOR_CACHE_DIR"] == "/runtime-cache/inductor"
    assert environment["FS2_FAST_START_MECHANISM"] == "regional-cache"
    assert metadata["annotations"]["fast-start.fs2.nebius/mechanism"] == "regional-cache"


def test_regional_cache_warms_the_retained_payload_pages() -> None:
    pod_spec, metadata = _conventional()
    configure_regional_cache(
        pod_spec=pod_spec,
        pod_metadata=metadata,
        qualification=_regional_cache(),
        runtime_image=QWEN_IMAGE,
        runtime_container_name="vllm",
    )
    warm = next(item for item in pod_spec["initContainers"] if item["name"] == "fs2-warm-page-cache")
    assert warm["image"] == QWEN_IMAGE
    environment = {item["name"]: item["value"] for item in warm["env"]}
    assert environment["FS2_WARM_ROOT"] == "/models/qwen3-8b/payload"
    assert warm["volumeMounts"] == [{"name": "model", "mountPath": "/models", "readOnly": True}]
    compile(warm["command"][2], "warm-page-cache", "exec")


def test_regional_cache_is_idempotent_and_omits_warming_when_undeclared() -> None:
    declaration = _regional_cache()
    pod_spec, metadata = _conventional()
    for _ in range(2):
        configure_regional_cache(
            pod_spec=pod_spec,
            pod_metadata=metadata,
            qualification=declaration,
            runtime_image=QWEN_IMAGE,
            runtime_container_name="vllm",
        )
    assert len([item for item in pod_spec["initContainers"] if item["name"] == "fs2-warm-page-cache"]) == 1
    assert len([item for item in pod_spec["volumes"] if item["name"] == "runtime-cache"]) == 1

    unwarmed = _regional_cache(warm_page_cache=None)
    pod_spec, metadata = _conventional()
    configure_regional_cache(
        pod_spec=pod_spec,
        pod_metadata=metadata,
        qualification=unwarmed,
        runtime_image=QWEN_IMAGE,
        runtime_container_name="vllm",
    )
    assert "initContainers" not in pod_spec


def test_regional_cache_refuses_an_image_that_is_not_the_in_region_mirror() -> None:
    pod_spec, metadata = _conventional()
    foreign = "docker.io/library/vllm@sha256:" + "cd" * 32
    with pytest.raises(FastStartMechanismError, match="in-region mirror"):
        configure_regional_cache(
            pod_spec=pod_spec,
            pod_metadata=metadata,
            qualification=_regional_cache(),
            runtime_image=foreign,
            runtime_container_name="vllm",
        )
    with pytest.raises(FastStartMechanismError, match="digest-pinned"):
        configure_regional_cache(
            pod_spec=pod_spec,
            pod_metadata=metadata,
            qualification=_regional_cache(),
            runtime_image="cr.eu-north1.nebius.cloud/fs2-models/vllm-openai:latest",
            runtime_container_name="vllm",
        )
