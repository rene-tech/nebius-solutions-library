"""Cold-start mechanism vocabulary and per-pool availability."""

from __future__ import annotations

import copy
import fcntl
import hashlib
import json
import os
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any
from unittest import mock

import pytest

from fs2_serve import residency_agent
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
    configure_gpu_resident,
    configure_host_memory_residency,
    configure_regional_cache,
    parse_memory_quantity,
    project_cache_mechanisms,
    residency_holder_manifests,
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
    assert set(blocked) == {"gpu-resident", "node-local-restore", "shared-restore"}
    # The report carries the exact selector the scheduler uses, so the claim is
    # checkable against the live pool rather than a separate capability list.
    assert blocked["node-local-restore"].evidence_selector == {"local-nvme.fs2.nebius/eligible": "false"}
    assert blocked["shared-restore"].evidence_selector == {"snapshot.fs2.nebius/eligible": "false"}


def test_only_production_implemented_mechanisms_are_available_on_the_h100_pool() -> None:
    assessed = _by_name("h100-reserved-8x", H100_RESERVED_SELECTOR)
    assert assessed["conventional"][0] == "Available"
    assert assessed["regional-cache"][0] == "Available"
    assert assessed["host-memory-residency"][0] == "Available"
    assert assessed["gpu-resident"] == ("Unavailable", "PromotionControllerNotInstalled")


def test_a_pool_with_local_nvme_unlocks_the_restore_paths() -> None:
    assessed = _by_name("hypothetical-nvme-8x", NVME_POOL_SELECTOR)
    assert assessed["node-local-restore"] == ("Available", "NodeLocalNvmePresent")
    assert assessed["shared-restore"] == ("Available", "SnapshotCapabilityPresent")
    blocked = unavailable_mechanisms(pool_id="hypothetical-nvme-8x", node_selector=NVME_POOL_SELECTOR)
    assert set(blocked) == {"gpu-resident"}


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
    )
    assert FastStartMechanism.GPU_RESIDENT not in SELECTABLE_MECHANISMS
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
    explicitly_large = _host_memory(reserved_bytes=1648745732096 // 2)
    assert explicitly_large.reserved_fraction_of_node == pytest.approx(0.5)
    with pytest.raises(ValueError, match="cannot exceed declared node allocatable"):
        _host_memory(reserved_bytes=1648745732097)


def test_status_projection_is_nonthrowing_for_oversized_external_host_state() -> None:
    malformed = _host_memory().model_copy(update={"reserved_bytes": 2 * 1024**4, "node_allocatable_bytes": 1024**4})
    projected = project_cache_mechanisms(
        selected=FastStartMechanism.HOST_MEMORY_RESIDENCY,
        declarations={FastStartMechanism.HOST_MEMORY_RESIDENCY: malformed},
        pools={"h100-reserved-8x": H100_RESERVED_SELECTOR},
        pool_allocatable_memory_bytes={"h100-reserved-8x": 1024**4},
        pool_max_nodes={"h100-reserved-8x": 1},
        host_residency_pool_refs={"h100-reserved-8x"},
        host_residency_ready=False,
        storage_contract_digests={},
        converged=True,
        configured_hot_replicas=0,
        mechanism_config_digest=mechanism_config_digest,
    )
    host = projected["host-memory-residency"]
    assert host.state == "Unavailable"
    assert host.reason == "HostMemoryReservationExceedsPool"
    assert host.reserved_host_memory_fraction is None


def test_sleep_offload_declaration_still_states_its_ram_requirement() -> None:
    declaration = _host_memory(mode="runtime-sleep-offload")
    assert declaration.residency_mode == "runtime-sleep-offload"
    # The engine holds its own offloaded weights, so no holder headroom applies,
    # but the reservation must still cover them.
    with pytest.raises(ValueError, match="cover the offloaded weights"):
        _host_memory(mode="runtime-sleep-offload", reserved_bytes=1024)


def test_sleep_offload_is_reported_unavailable_until_an_actor_exists() -> None:
    declaration = _host_memory(mode="runtime-sleep-offload")
    projected = project_cache_mechanisms(
        selected=FastStartMechanism.HOST_MEMORY_RESIDENCY,
        declarations={FastStartMechanism.HOST_MEMORY_RESIDENCY: declaration},
        pools={"h100-reserved-8x": H100_RESERVED_SELECTOR},
        pool_allocatable_memory_bytes={"h100-reserved-8x": declaration.node_allocatable_bytes},
        storage_contract_digests={},
        converged=True,
        configured_hot_replicas=0,
        mechanism_config_digest=mechanism_config_digest,
    )
    host = projected["host-memory-residency"]
    assert (host.state, host.reason, host.selected) == (
        "Unavailable",
        "SleepWakeControllerNotInstalled",
        False,
    )


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


def test_host_memory_residency_waits_for_the_holder_receipt_on_its_exact_node() -> None:
    pod_spec, metadata = _conventional()
    declaration = _host_memory()
    configure_host_memory_residency(
        pod_spec=pod_spec,
        pod_metadata=metadata,
        qualification=declaration,
        runtime_image=QWEN_IMAGE,
        model_ref="qwen3-8b",
        holder_identity="fsm-hostmem-qwen3-8b",
        runtime_container_name="vllm",
    )
    # Avoiding required inter-Pod affinity lets the serving Pod trigger a
    # scale-up from zero. The DaemonSet follows it and this node-scoped receipt
    # remains the admission barrier.
    assert "podAffinity" not in pod_spec.get("affinity", {})

    # And it refuses to start unless this node's receipt proves the exact
    # configuration, payload digest and byte count are resident.
    verify = next(item for item in pod_spec["initContainers"] if item["name"] == "fs2-verify-host-memory-residency")
    environment = {item["name"]: item.get("value") for item in verify["env"]}
    assert environment["FS2_RESIDENCY_CONFIG_DIGEST"] == declaration.config_digest
    assert environment["FS2_RESIDENCY_PAYLOAD_DIGEST"] == declaration.payload_digest
    assert environment["FS2_RESIDENCY_BYTES"] == str(declaration.payload_bytes)
    assert environment["FS2_RESIDENCY_RECEIPT_ROOT"] == "/residency"
    assert environment["FS2_RESIDENCY_HOLDER_ID"] == "fsm-hostmem-qwen3-8b"
    node_ref = next(item for item in verify["env"] if item["name"] == "FS2_NODE_NAME")
    assert node_ref["valueFrom"]["fieldRef"]["fieldPath"] == "spec.nodeName"
    receipt_volume = next(item for item in pod_spec["volumes"] if item["name"] == "residency-receipt")
    assert receipt_volume["persistentVolumeClaim"]["readOnly"] is True
    assert verify["volumeMounts"] == [{"name": "residency-receipt", "mountPath": "/residency", "readOnly": True}]
    compile(verify["command"][2], "residency-verify", "exec")
    assert metadata["annotations"]["fast-start.fs2.nebius/reserved-host-memory-bytes"] == "19327352832"


def test_runtime_admission_rejects_a_fresh_receipt_from_a_dead_holder(tmp_path: Path) -> None:
    pod_spec, metadata = _conventional()
    declaration = _host_memory()
    configure_host_memory_residency(
        pod_spec=pod_spec,
        pod_metadata=metadata,
        qualification=declaration,
        runtime_image=QWEN_IMAGE,
        model_ref="qwen3-8b",
        holder_identity="fsm-hostmem-qwen3-8b",
        runtime_container_name="vllm",
    )
    verify = next(item for item in pod_spec["initContainers"] if item["name"] == "fs2-verify-host-memory-residency")
    receipt_root = tmp_path / "residency"
    receipt_directory = receipt_root / "fsm-hostmem-qwen3-8b" / "node-a"
    incarnation = "terminated-pod-uid"
    lock_path = receipt_directory / "incarnations" / f"{incarnation}.lock"
    lock_path.parent.mkdir(parents=True)
    lock_path.touch()
    (receipt_directory / "receipt.json").write_text(
        json.dumps(
            {
                "schema": "fs2-serve.nebius.ai/fast-start-host-memory-residency-receipt/v2",
                "holder_id": "fsm-hostmem-qwen3-8b",
                "holder_incarnation": incarnation,
                "node_name": "node-a",
                "config_digest": declaration.config_digest,
                "payload_digest": declaration.payload_digest,
                "content_digest_verified": True,
                "resident_bytes": declaration.payload_bytes,
                "refreshed_at_epoch": 4102444800.0,
            }
        )
    )
    environment = {
        **os.environ,
        "FS2_RESIDENCY_RECEIPT_ROOT": str(receipt_root),
        "FS2_RESIDENCY_HOLDER_ID": "fsm-hostmem-qwen3-8b",
        "FS2_RESIDENCY_CONFIG_DIGEST": declaration.config_digest,
        "FS2_RESIDENCY_PAYLOAD_DIGEST": declaration.payload_digest,
        "FS2_RESIDENCY_BYTES": str(declaration.payload_bytes),
        "FS2_RESIDENCY_MAX_AGE_SECONDS": "180",
        "FS2_RESIDENCY_WAIT_SECONDS": "0",
        "FS2_NODE_NAME": "node-a",
    }

    result = subprocess.run(  # noqa: S603 - fixed interpreter and test-owned inline script
        [sys.executable, "-c", verify["command"][2]],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
        timeout=5,
    )

    assert result.returncode == 1
    assert "receipt_holder_incarnation_not_live" in result.stderr


def test_runtime_admission_accepts_a_live_holder_through_a_read_only_lock_file(tmp_path: Path) -> None:
    """Exercise the exact read-only access mode rendered into serving Pods."""

    pod_spec, metadata = _conventional()
    declaration = _host_memory()
    configure_host_memory_residency(
        pod_spec=pod_spec,
        pod_metadata=metadata,
        qualification=declaration,
        runtime_image=QWEN_IMAGE,
        model_ref="qwen3-8b",
        holder_identity="fsm-hostmem-qwen3-8b",
        runtime_container_name="vllm",
    )
    verify = next(item for item in pod_spec["initContainers"] if item["name"] == "fs2-verify-host-memory-residency")
    receipt_root = tmp_path / "residency"
    receipt_directory = receipt_root / "fsm-hostmem-qwen3-8b" / "node-a"
    incarnation = "live-holder-pod-uid"
    lock_path = receipt_directory / "incarnations" / f"{incarnation}.lock"
    lock_path.parent.mkdir(parents=True)
    lock_path.touch()
    descriptor = os.open(lock_path, os.O_RDWR)
    fcntl.flock(descriptor, fcntl.LOCK_EX)
    # A Kubernetes readOnly volume denies O_RDWR even when the file itself was
    # created by the holder.  Mode 0400 gives the subprocess the same relevant
    # file-access boundary without requiring a privileged test mount.
    lock_path.chmod(0o400)
    (receipt_directory / "receipt.json").write_text(
        json.dumps(
            {
                "schema": "fs2-serve.nebius.ai/fast-start-host-memory-residency-receipt/v2",
                "holder_id": "fsm-hostmem-qwen3-8b",
                "holder_incarnation": incarnation,
                "node_name": "node-a",
                "config_digest": declaration.config_digest,
                "payload_digest": declaration.payload_digest,
                "content_digest_verified": True,
                "resident_bytes": declaration.payload_bytes,
                "refreshed_at_epoch": 4102444800.0,
            }
        )
    )
    environment = {
        **os.environ,
        "FS2_RESIDENCY_RECEIPT_ROOT": str(receipt_root),
        "FS2_RESIDENCY_HOLDER_ID": "fsm-hostmem-qwen3-8b",
        "FS2_RESIDENCY_CONFIG_DIGEST": declaration.config_digest,
        "FS2_RESIDENCY_PAYLOAD_DIGEST": declaration.payload_digest,
        "FS2_RESIDENCY_BYTES": str(declaration.payload_bytes),
        "FS2_RESIDENCY_MAX_AGE_SECONDS": "180",
        "FS2_RESIDENCY_WAIT_SECONDS": "0",
        "FS2_NODE_NAME": "node-a",
    }
    try:
        result = subprocess.run(  # noqa: S603 - fixed interpreter and test-owned inline script
            [sys.executable, "-c", verify["command"][2]],
            check=False,
            capture_output=True,
            text=True,
            env=environment,
            timeout=5,
        )
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["admitted"] is True


def test_the_residency_holder_schedules_the_ram_it_costs() -> None:
    declaration = _host_memory()
    manifests = residency_holder_manifests(
        namespace="fs2-models",
        name="fsm-hostmem-qwen3-8b",
        model_ref="qwen3-8b",
        holder_identity="fsm-hostmem-qwen3-8b",
        qualification=declaration,
        image=QWEN_IMAGE,
        node_selector={"kubernetes.io/hostname": "h100-node-a"},
        tolerations=[{"key": "dedicated", "operator": "Equal", "value": "fs2-inference", "effect": "NoSchedule"}],
        labels={},
        annotations={},
    )
    kinds = [item["kind"] for item in manifests]
    assert kinds == ["ConfigMap", "DaemonSet"]
    agent = manifests[0]["data"]["residency_agent.py"]
    compile(agent, "residency-agent", "exec")

    container = manifests[1]["spec"]["template"]["spec"]["containers"][0]
    # Request and limit are both the declared reservation, so the node RAM this
    # mechanism costs is scheduled and attributable, not incidental.
    assert container["resources"]["requests"]["memory"] == "19327352832"
    assert container["resources"]["limits"]["memory"] == "19327352832"
    assert container["securityContext"]["capabilities"]["add"] == ["IPC_LOCK"]
    incarnation_ref = next(item for item in container["env"] if item["name"] == "FS2_RESIDENCY_HOLDER_INCARNATION")
    assert incarnation_ref["valueFrom"]["fieldRef"]["fieldPath"] == "metadata.uid"
    assert (
        manifests[1]["spec"]["template"]["metadata"]["labels"]["fast-start.fs2.nebius/host-memory-residency"]
        == "qwen3-8b"
    )
    mounts = {item["name"]: item["mountPath"] for item in container["volumeMounts"]}
    assert mounts == {"agent": "/agent", "payload": "/models", "receipt": "/residency"}


def test_a_mapped_residency_holder_does_not_ask_for_the_lock_capability() -> None:
    manifests = residency_holder_manifests(
        namespace="fs2-models",
        name="fsm-hostmem-qwen3-8b",
        model_ref="qwen3-8b",
        holder_identity="fsm-hostmem-qwen3-8b",
        qualification=_host_memory(mode="mapped-payload-residency"),
        image=QWEN_IMAGE,
        node_selector={"kubernetes.io/hostname": "node"},
        tolerations=[],
        labels={},
        annotations={},
    )
    capabilities = manifests[1]["spec"]["template"]["spec"]["containers"][0]["securityContext"]["capabilities"]
    assert capabilities == {"drop": ["ALL"]}


def test_sleep_offload_is_not_renderable_without_a_lifecycle_actor() -> None:
    pod_spec, metadata = _conventional()
    declaration = _host_memory(mode="runtime-sleep-offload")
    with pytest.raises(FastStartMechanismError, match="sleep/wake controller"):
        configure_host_memory_residency(
            pod_spec=pod_spec,
            pod_metadata=metadata,
            qualification=declaration,
            runtime_image=QWEN_IMAGE,
            model_ref="qwen3-8b",
            holder_identity="fsm-hostmem-qwen3-8b",
            runtime_container_name="vllm",
        )
    assert "initContainers" not in pod_spec
    assert "affinity" not in pod_spec
    assert "resources" not in pod_spec["containers"][0]
    assert metadata["annotations"] == {}

    with pytest.raises(FastStartMechanismError, match="held by the runtime"):
        residency_holder_manifests(
            namespace="fs2-models",
            name="fsm-hostmem-qwen3-8b",
            model_ref="qwen3-8b",
            holder_identity="fsm-hostmem-qwen3-8b",
            qualification=declaration,
            image=QWEN_IMAGE,
            node_selector={},
            tolerations=[],
            labels={},
            annotations={},
        )


def test_kubernetes_memory_quantities_used_by_reservations_are_exact() -> None:
    assert parse_memory_quantity("64Gi") == 68719476736
    assert parse_memory_quantity("1000M") == 1000000000


def test_the_residency_agent_holds_real_bytes_and_publishes_a_verifiable_receipt(tmp_path: Path) -> None:
    """Exercise the packaged agent end to end against a real payload tree."""

    payload_root = tmp_path / "payload"
    payload_root.mkdir()
    (payload_root / "shard-0.bin").write_bytes(b"a" * 8192)
    (payload_root / "shard-1.bin").write_bytes(b"b" * 4096)
    receipt_root = tmp_path / "residency"
    environment = {
        "FS2_RESIDENCY_MODEL_REF": "qwen3-8b",
        "FS2_RESIDENCY_HOLDER_ID": "fsm-hostmem-qwen3-8b",
        # Locking needs CAP_IPC_LOCK, which a test process does not have; the
        # mapped mode is the same code path minus the lock syscall.
        "FS2_RESIDENCY_MODE": "mapped-payload-residency",
        "FS2_RESIDENCY_PAYLOAD_ROOT": str(payload_root),
        "FS2_RESIDENCY_PAYLOAD_DIGEST": residency_agent.localized_content_digest(payload_root),
        "FS2_RESIDENCY_PAYLOAD_BYTES": "12288",
        "FS2_RESIDENCY_RESERVED_BYTES": "16384",
        "FS2_RESIDENCY_CONFIG_DIGEST": "sha256:" + "7c" * 32,
        "FS2_RESIDENCY_RECEIPT_ROOT": str(receipt_root),
        "FS2_RESIDENCY_REFRESH_SECONDS": "30",
        "FS2_NODE_NAME": "h100-node-a",
        "FS2_RESIDENCY_HOLDER_INCARNATION": "holder-pod-uid-a",
    }
    receipt_ready = threading.Event()
    release_holder = threading.Event()
    holder_errors: list[Exception] = []

    def block_while_holder_is_live(_seconds: float) -> None:
        receipt_ready.set()
        assert release_holder.wait(timeout=5)
        raise StopIteration

    def run_holder() -> None:
        try:
            residency_agent.hold()
        except Exception as exc:  # The thread carries the intentional StopIteration to the test.
            holder_errors.append(exc)

    with mock.patch.dict(os.environ, environment, clear=False):
        with mock.patch.object(residency_agent.time, "sleep", side_effect=block_while_holder_is_live):
            holder = threading.Thread(target=run_holder)
            holder.start()
            assert receipt_ready.wait(timeout=5)
            receipt_path = receipt_root / "fsm-hostmem-qwen3-8b" / "h100-node-a" / "receipt.json"
            receipt = json.loads(receipt_path.read_text())
            assert receipt["resident_bytes"] == 12288
            assert receipt["resident_files"] == 2
            assert receipt["content_digest_verified"] is True
            assert receipt["residency_guaranteed"] is False
            assert receipt["holder_incarnation"] == "holder-pod-uid-a"
            assert receipt["node_name"] == "h100-node-a"
            # The same receipt is what the readiness probe and the runtime's
            # init container check, including the holder's live incarnation.
            assert residency_agent.check() == 0

            stale = json.loads(receipt_path.read_text())
            stale["refreshed_at_epoch"] = 0.0
            receipt_path.write_text(json.dumps(stale))
            assert residency_agent.check() == 1
            receipt_path.write_text(json.dumps(receipt))
            release_holder.set()
            holder.join(timeout=5)
            assert not holder.is_alive()
        assert len(holder_errors) == 1 and isinstance(holder_errors[0], StopIteration)
        # A fresh receipt from the terminated predecessor cannot make its
        # replacement Ready because its exact incarnation lock is no longer held.
        assert residency_agent.check() == 1


def test_localized_digest_is_the_existing_artifact_manifest_content_identity(tmp_path: Path) -> None:
    payload_root = tmp_path / "payload"
    payload_root.mkdir()
    (payload_root / "a.bin").write_bytes(b"alpha")
    nested = payload_root / "nested"
    nested.mkdir()
    (nested / "b.bin").write_bytes(b"beta")
    inventory = [
        {"path": "a.bin", "bytes": 5, "sha256": hashlib.sha256(b"alpha").hexdigest()},
        {"path": "nested/b.bin", "bytes": 4, "sha256": hashlib.sha256(b"beta").hexdigest()},
    ]
    canonical = (
        json.dumps(
            inventory,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode()
        + b"\n"
    )

    assert residency_agent.localized_content_digest(payload_root) == f"sha256:{hashlib.sha256(canonical).hexdigest()}"


def test_restarted_holder_invalidates_its_predecessor_receipt_before_hashing(tmp_path: Path) -> None:
    payload_root = tmp_path / "payload"
    payload_root.mkdir()
    (payload_root / "shard.bin").write_bytes(b"payload")
    receipt_root = tmp_path / "residency"
    receipt_path = receipt_root / "holder" / "node" / "receipt.json"
    receipt_path.parent.mkdir(parents=True)
    receipt_path.write_text('{"refreshed_at_epoch":9999999999}\n')
    environment = {
        "FS2_RESIDENCY_MODEL_REF": "qwen3-8b",
        "FS2_RESIDENCY_HOLDER_ID": "holder",
        "FS2_RESIDENCY_MODE": "mapped-payload-residency",
        "FS2_RESIDENCY_PAYLOAD_ROOT": str(payload_root),
        "FS2_RESIDENCY_PAYLOAD_DIGEST": "sha256:" + "5b" * 32,
        "FS2_RESIDENCY_PAYLOAD_BYTES": "7",
        "FS2_RESIDENCY_RESERVED_BYTES": "4096",
        "FS2_RESIDENCY_CONFIG_DIGEST": "sha256:" + "7c" * 32,
        "FS2_RESIDENCY_RECEIPT_ROOT": str(receipt_root),
        "FS2_NODE_NAME": "node",
        "FS2_RESIDENCY_HOLDER_INCARNATION": "holder-pod-uid-b",
    }

    def stop_after_invalidation(_root: Path) -> str:
        assert not receipt_path.exists()
        raise residency_agent.ResidencyError("stop after receipt invalidation")

    with mock.patch.dict(os.environ, environment, clear=False):
        with mock.patch.object(residency_agent, "localized_content_digest", side_effect=stop_after_invalidation):
            with pytest.raises(residency_agent.ResidencyError, match="stop after receipt invalidation"):
                residency_agent.hold()
    assert not receipt_path.exists()


def test_the_residency_agent_refuses_to_understate_what_it_holds(tmp_path: Path) -> None:
    payload_root = tmp_path / "payload"
    payload_root.mkdir()
    (payload_root / "shard-0.bin").write_bytes(b"a" * 1024)
    environment = {
        "FS2_RESIDENCY_MODEL_REF": "qwen3-8b",
        "FS2_RESIDENCY_HOLDER_ID": "fsm-hostmem-qwen3-8b",
        "FS2_RESIDENCY_MODE": "mapped-payload-residency",
        "FS2_RESIDENCY_PAYLOAD_ROOT": str(payload_root),
        "FS2_RESIDENCY_PAYLOAD_DIGEST": residency_agent.localized_content_digest(payload_root),
        # The declaration claims more bytes than the tree actually holds.
        "FS2_RESIDENCY_PAYLOAD_BYTES": "99999",
        "FS2_RESIDENCY_RESERVED_BYTES": "200000",
        "FS2_RESIDENCY_CONFIG_DIGEST": "sha256:" + "7c" * 32,
        "FS2_RESIDENCY_RECEIPT_ROOT": str(tmp_path / "residency"),
        "FS2_NODE_NAME": "node",
        "FS2_RESIDENCY_HOLDER_INCARNATION": "holder-pod-uid-c",
    }
    with mock.patch.dict(os.environ, environment, clear=False):
        with pytest.raises(residency_agent.ResidencyError, match="of the declared"):
            residency_agent.hold()


def test_the_residency_agent_rejects_same_size_wrong_content(tmp_path: Path) -> None:
    payload_root = tmp_path / "payload"
    payload_root.mkdir()
    shard = payload_root / "shard.bin"
    shard.write_bytes(b"a" * 4096)
    expected_digest = residency_agent.localized_content_digest(payload_root)
    shard.write_bytes(b"b" * 4096)
    receipt_root = tmp_path / "residency"
    environment = {
        "FS2_RESIDENCY_MODEL_REF": "qwen3-8b",
        "FS2_RESIDENCY_HOLDER_ID": "fsm-hostmem-qwen3-8b",
        "FS2_RESIDENCY_MODE": "mapped-payload-residency",
        "FS2_RESIDENCY_PAYLOAD_ROOT": str(payload_root),
        "FS2_RESIDENCY_PAYLOAD_DIGEST": expected_digest,
        "FS2_RESIDENCY_PAYLOAD_BYTES": "4096",
        "FS2_RESIDENCY_RESERVED_BYTES": "8192",
        "FS2_RESIDENCY_CONFIG_DIGEST": "sha256:" + "7c" * 32,
        "FS2_RESIDENCY_RECEIPT_ROOT": str(receipt_root),
        "FS2_NODE_NAME": "node",
        "FS2_RESIDENCY_HOLDER_INCARNATION": "holder-pod-uid-d",
    }
    with mock.patch.dict(os.environ, environment, clear=False):
        with pytest.raises(residency_agent.ResidencyError, match="does not match declared"):
            residency_agent.hold()
    assert not (receipt_root / "fsm-hostmem-qwen3-8b" / "node" / "receipt.json").exists()


def test_the_residency_probe_fails_closed_when_the_receipt_is_missing(tmp_path: Path) -> None:
    environment = {
        "FS2_RESIDENCY_MODEL_REF": "qwen3-8b",
        "FS2_RESIDENCY_HOLDER_ID": "fsm-hostmem-qwen3-8b",
        "FS2_RESIDENCY_PAYLOAD_DIGEST": "sha256:" + "5b" * 32,
        "FS2_RESIDENCY_PAYLOAD_BYTES": "4096",
        "FS2_RESIDENCY_CONFIG_DIGEST": "sha256:" + "7c" * 32,
        "FS2_RESIDENCY_RECEIPT_ROOT": str(tmp_path / "missing"),
        "FS2_NODE_NAME": "node",
        "FS2_RESIDENCY_HOLDER_INCARNATION": "holder-pod-uid-e",
    }
    with mock.patch.dict(os.environ, environment, clear=False):
        assert residency_agent.check() == 1


def test_gpu_resident_parks_a_standby_engine_behind_a_readiness_gate() -> None:
    pod_spec, metadata = _conventional()
    pod_spec["containers"][0]["readinessProbe"] = {
        "httpGet": {"path": "/health", "port": "http"},
        "periodSeconds": 5,
    }
    declaration = _gpu_resident()
    configure_gpu_resident(
        pod_spec=pod_spec,
        pod_metadata=metadata,
        qualification=declaration,
        configured_hot_replicas=1,
        role="standby",
        runtime_container_name="vllm",
    )
    # The engine loads and keeps its weights in GPU memory, but the gate holds
    # it out of the Service until it is promoted, so activation is a promotion.
    assert pod_spec["readinessGates"] == [{"conditionType": "fast-start.fs2.nebius/promoted"}]
    assert pod_spec["containers"][0]["readinessProbe"]["periodSeconds"] == 1
    assert metadata["labels"]["fast-start.fs2.nebius/gpu-resident-role"] == "standby"
    annotations = metadata["annotations"]
    assert annotations["fast-start.fs2.nebius/gpu-resident-standby-replicas"] == "1"
    assert annotations["fast-start.fs2.nebius/gpu-resident-minimum-hot-replicas"] == "1"
    assert annotations["fast-start.fs2.nebius/gpu-resident-reserved-accelerators"] == "1"


def test_a_promoted_serving_replica_carries_no_gate() -> None:
    pod_spec, metadata = _conventional()
    configure_gpu_resident(
        pod_spec=pod_spec,
        pod_metadata=metadata,
        qualification=_gpu_resident(),
        configured_hot_replicas=1,
        role="serving",
        runtime_container_name="vllm",
    )
    assert "readinessGates" not in pod_spec
    assert metadata["labels"]["fast-start.fs2.nebius/gpu-resident-role"] == "serving"


def test_gpu_resident_refuses_a_hot_floor_that_cannot_afford_it() -> None:
    """The relationship to min hot replicas is explicit, never implicit."""

    pod_spec, metadata = _conventional()
    with pytest.raises(FastStartMechanismError, match="hot floor"):
        configure_gpu_resident(
            pod_spec=pod_spec,
            pod_metadata=metadata,
            qualification=_gpu_resident(minimum_hot_replicas=2),
            configured_hot_replicas=1,
            role="standby",
            runtime_container_name="vllm",
        )


def test_gpu_resident_is_idempotent() -> None:
    pod_spec, metadata = _conventional()
    for _ in range(2):
        configure_gpu_resident(
            pod_spec=pod_spec,
            pod_metadata=metadata,
            qualification=_gpu_resident(),
            configured_hot_replicas=1,
            role="standby",
            runtime_container_name="vllm",
        )
    assert pod_spec["readinessGates"] == [{"conditionType": "fast-start.fs2.nebius/promoted"}]


def test_the_holder_reads_the_payload_as_the_runtime_does() -> None:
    """The holder must carry the runtime's identity, not its own.

    A retained payload on a shared filesystem is owned by the runtime's user and
    its directories are not world-readable. A holder that ran as a different
    identity, or as root with all capabilities dropped, would be denied exactly
    the files it is supposed to hold resident. This was observed live before it
    was fixed: the payload directory is mode 0750 owned by uid 1000, and an
    otherwise correct Pod running as root with `drop: [ALL]` could not traverse
    it.
    """

    runtime_identity = {
        "runAsUser": 1000,
        "runAsGroup": 1000,
        "runAsNonRoot": True,
        "fsGroup": 1000,
        "seccompProfile": {"type": "RuntimeDefault"},
    }
    manifests = residency_holder_manifests(
        namespace="fs2-models",
        name="fsm-hostmem-qwen3-8b",
        model_ref="qwen3-8b",
        holder_identity="fsm-hostmem-qwen3-8b",
        qualification=_host_memory(),
        image=QWEN_IMAGE,
        node_selector={"kubernetes.io/hostname": "node"},
        tolerations=[],
        labels={},
        annotations={},
        pod_security_context=runtime_identity,
    )
    pod = manifests[1]["spec"]["template"]["spec"]
    assert pod["securityContext"] == runtime_identity
    # Locking still needs the capability, and a non-root user can hold it.
    assert pod["containers"][0]["securityContext"]["capabilities"]["add"] == ["IPC_LOCK"]


def test_a_holder_without_a_runtime_identity_declares_none() -> None:
    manifests = residency_holder_manifests(
        namespace="fs2-models",
        name="fsm-hostmem-qwen3-8b",
        model_ref="qwen3-8b",
        holder_identity="fsm-hostmem-qwen3-8b",
        qualification=_host_memory(),
        image=QWEN_IMAGE,
        node_selector={"kubernetes.io/hostname": "node"},
        tolerations=[],
        labels={},
        annotations={},
    )
    assert "securityContext" not in manifests[1]["spec"]["template"]["spec"]
