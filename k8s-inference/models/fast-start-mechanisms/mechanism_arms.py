#!/usr/bin/env python3
"""Render the live campaign arms through the production mechanism code.

Every candidate arm is produced by calling the same
``fs2_serve.fast_start_mechanisms`` functions the ModelDeployment renderer
calls, so a measured improvement is attributable to the shipped mechanism and
not to a hand-written benchmark fixture.  The control arm is the conventional
render: the retained payload mounted read-only and a discarded ``emptyDir``
compile cache, exactly as the model serves today.

The arms are plain Pods plus one Service each.  A Pod is the smallest object
that reproduces the production Pod template, and it lets the campaign cycle an
attempt without touching any live Deployment, ScaledObject or Kueue workload.
"""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path
from typing import Any

SOLUTION_ROOT = Path(__file__).resolve().parents[2]
CONTROL_PLANE_SOURCE = SOLUTION_ROOT / "components/control-plane/src"
if str(CONTROL_PLANE_SOURCE) not in sys.path:
    sys.path.insert(0, str(CONTROL_PLANE_SOURCE))

from fs2_serve.fast_start_identity import mechanism_config_digest  # noqa: E402
from fs2_serve.fast_start_mechanisms import (  # noqa: E402
    GPU_RESIDENT_READINESS_GATE,
    HOST_MEMORY_RESIDENCY_LABEL,
    FastStartMechanism,
    GpuResidentQualification,
    HostMemoryResidencyQualification,
    RegionalCacheQualification,
    RetainedCompileCache,
    ResidencyHolder,
    WarmPageCacheReadAhead,
    canonical_digest,
    configure_gpu_resident,
    configure_host_memory_residency,
    configure_regional_cache,
    residency_holder_manifests,
)

CONTRACT_PATH = Path(__file__).with_name("campaign-contract.json")
CAMPAIGN_LABEL = "fast-start.fs2.nebius/campaign"
ARM_LABEL = "fast-start.fs2.nebius/arm"
ATTEMPT_LABEL = "fast-start.fs2.nebius/attempt"

ARMS: tuple[str, ...] = (
    "conventional",
    "regional-cache",
    "host-memory-residency",
    "host-memory-residency-sleep-offload",
    "gpu-resident",
)
#: Arms whose activation is a promotion of an already-parked replica rather
#: than a cold load.  Their reported time is activation, not cold start, and the
#: campaign records the accelerator or host RAM the parked replica holds.
PROMOTION_ARMS: frozenset[str] = frozenset({"host-memory-residency-sleep-offload", "gpu-resident"})


class ArmError(RuntimeError):
    """An arm cannot be rendered from this contract."""


def load_contract(path: Path = CONTRACT_PATH) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def target(contract: dict[str, Any], name: str) -> dict[str, Any]:
    targets = contract.get("targets", {})
    if name not in targets:
        raise ArmError(f"campaign contract has no target named {name}")
    selected = targets[name]
    if selected.get("state") != "runnable":
        raise ArmError(f"target {name} is not runnable: {selected.get('blocker', 'no reason recorded')}")
    return dict(selected)


def regional_cache_declaration(spec: dict[str, Any]) -> RegionalCacheQualification:
    """Build the reviewed regional-cache declaration for this target."""

    compile_cache = RetainedCompileCache(
        claim_name=spec["compile_cache_claim_name"],
        # The ABI is part of the sub-path, so a driver or architecture change
        # cannot read a cache built by an incompatible stack.
        sub_path=f"{spec['model_ref']}/{spec['compile_cache_abi']}",
        abi=spec["compile_cache_abi"],
        mount_path="/runtime-cache",
        size_limit_bytes=16 * 1024**3,
    )
    warm = WarmPageCacheReadAhead(workers=16, read_bytes_limit=spec["payload_bytes"], timeout_seconds=600)
    payload = {
        "schema": "fs2-serve.nebius.ai/fast-start-regional-cache/v1",
        "imageMirrorRegistry": spec["image_mirror_registry"],
        "payloadClaimName": spec["payload_claim_name"],
        "payloadContentPath": spec["payload_content_path"],
        "payloadBytes": spec["payload_bytes"],
        "compileCache": compile_cache.model_dump(mode="json", by_alias=True),
        "warmPageCache": warm.model_dump(mode="json", by_alias=True),
        "poolRefs": [spec["pool_ref"]],
    }
    return RegionalCacheQualification(
        config_digest=canonical_digest(payload),
        image_mirror_registry=spec["image_mirror_registry"],
        payload_claim_name=spec["payload_claim_name"],
        payload_content_path=spec["payload_content_path"],
        payload_bytes=spec["payload_bytes"],
        compile_cache=compile_cache,
        warm_page_cache=warm,
        pool_refs=[spec["pool_ref"]],
    )


def host_memory_declaration(spec: dict[str, Any], *, mode: str) -> HostMemoryResidencyQualification:
    """Build the reviewed host-memory-residency declaration for this target."""

    holder = ResidencyHolder(
        name=f"fsm-hostmem-{spec['model_ref']}",
        namespace=spec["namespace"],
        receipt_claim_name=spec["residency_receipt_claim_name"],
        receipt_mount_path="/residency",
    )
    payload = {
        "schema": "fs2-serve.nebius.ai/fast-start-host-memory-residency/v1",
        "residencyMode": mode,
        "payloadClaimName": spec["payload_claim_name"],
        "payloadContentPath": spec["payload_content_path"],
        "payloadDigest": spec["payload_digest"],
        "payloadBytes": spec["payload_bytes"],
        "reservedBytes": spec["residency_reserved_bytes"],
        "nodeAllocatableBytes": spec["node_allocatable_bytes"],
        "holder": holder.model_dump(mode="json", by_alias=True),
        "receiptMaxAgeSeconds": 180,
        "poolRefs": [spec["pool_ref"]],
    }
    return HostMemoryResidencyQualification(
        config_digest=canonical_digest(payload),
        residency_mode=mode,
        payload_claim_name=spec["payload_claim_name"],
        payload_content_path=spec["payload_content_path"],
        payload_digest=spec["payload_digest"],
        payload_bytes=spec["payload_bytes"],
        reserved_bytes=spec["residency_reserved_bytes"],
        node_allocatable_bytes=spec["node_allocatable_bytes"],
        holder=holder,
        receipt_max_age_seconds=180,
        pool_refs=[spec["pool_ref"]],
    )


def gpu_resident_declaration(spec: dict[str, Any]) -> GpuResidentQualification:
    """Build the reviewed gpu-resident declaration for this target.

    ``minimumHotReplicas`` is 1 because a parked standby engine only makes
    sense next to a paid hot floor: the mechanism trades a permanently held
    accelerator for an activation that is a promotion instead of a load.
    """

    payload = {
        "schema": "fs2-serve.nebius.ai/fast-start-gpu-resident/v1",
        "residencyMode": "standby-engine",
        "standbyReplicas": 1,
        "acceleratorsPerStandbyReplica": spec["accelerators_per_replica"],
        "minimumHotReplicas": 1,
        "promotionProbePeriodSeconds": 1,
        "poolRefs": [spec["pool_ref"]],
    }
    return GpuResidentQualification(
        config_digest=canonical_digest(payload),
        residency_mode="standby-engine",
        standby_replicas=1,
        accelerators_per_standby_replica=spec["accelerators_per_replica"],
        minimum_hot_replicas=1,
        promotion_probe_period_seconds=1,
        pool_refs=[spec["pool_ref"]],
    )


def declaration_for(spec: dict[str, Any], arm: str) -> Any:
    if arm == "conventional":
        return None
    if arm == "regional-cache":
        return regional_cache_declaration(spec)
    if arm == "host-memory-residency":
        return host_memory_declaration(spec, mode=spec["residency_mode"])
    if arm == "host-memory-residency-sleep-offload":
        return host_memory_declaration(spec, mode="runtime-sleep-offload")
    if arm == "gpu-resident":
        return gpu_resident_declaration(spec)
    raise ArmError(f"unknown campaign arm {arm}")


def mechanism_name(arm: str) -> str:
    if arm.startswith("host-memory-residency"):
        return FastStartMechanism.HOST_MEMORY_RESIDENCY.value
    return arm


def storage_contract_digest(spec: dict[str, Any]) -> str:
    """The exact retained-storage identity every arm of this target shares."""

    return canonical_digest(
        {
            "schema": "fs2-serve.nebius.ai/fast-start-storage-contract/v1",
            "storageClass": "csi-mounted-fs-path-sc",
            "storageMode": "rwx-filesystem",
            "claimName": spec["payload_claim_name"],
            "contentPath": spec["payload_content_path"],
        }
    )


def expected_mechanism_config_digest(spec: dict[str, Any], arm: str) -> str:
    declaration = declaration_for(spec, arm)
    return str(
        mechanism_config_digest(
            mechanism=mechanism_name(arm),
            storage_contract_digest=storage_contract_digest(spec),
            declaration_digest=None if declaration is None else declaration.config_digest,
        )
    )


def _base_pod(spec: dict[str, Any], *, name: str, labels: dict[str, str]) -> dict[str, Any]:
    """The conventional render: retained payload read-only, discarded JIT cache."""

    payload_mount = spec["payload_mount_path"]
    return {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {
            "name": name,
            "namespace": spec["namespace"],
            "labels": dict(labels),
            "annotations": {
                "fs2.nebius/model-content-digest": spec["payload_digest"],
                "fs2.nebius/runtime-image-digest": spec["runtime_image"].split("@", 1)[1],
                "fs2.nebius/compile-cache-abi": spec["compile_cache_abi"],
                "fs2.nebius/cache-content-path": spec["payload_content_path"],
            },
        },
        "spec": {
            "restartPolicy": "Never",
            "terminationGracePeriodSeconds": 5,
            # The retained payload on the shared filesystem is owned by the
            # runtime's user. The live render runs as that user; an arm that ran
            # as root with all capabilities dropped would lose the permission
            # bypass and be denied the payload it is meant to load.
            "securityContext": {
                "runAsUser": 1000,
                "runAsGroup": 1000,
                "runAsNonRoot": True,
                "fsGroup": 1000,
                "fsGroupChangePolicy": "OnRootMismatch",
                "seccompProfile": {"type": "RuntimeDefault"},
            },
            "nodeSelector": {"kubernetes.io/hostname": spec["node_name"]},
            "tolerations": [
                {"key": "dedicated", "operator": "Equal", "value": "fs2-inference", "effect": "NoSchedule"}
            ],
            "containers": [
                {
                    "name": spec["runtime_container_name"],
                    "image": spec["runtime_image"],
                    "args": list(spec["runtime_args"]),
                    "env": [
                        {"name": "HF_HUB_OFFLINE", "value": "1"},
                        {"name": "TRANSFORMERS_OFFLINE", "value": "1"},
                        {"name": "VLLM_NO_USAGE_STATS", "value": "1"},
                        {"name": "VLLM_CACHE_ROOT", "value": "/runtime-cache/vllm"},
                        {"name": "TRITON_CACHE_DIR", "value": "/runtime-cache/triton"},
                    ],
                    "ports": [{"name": "http", "containerPort": spec["service_port"], "protocol": "TCP"}],
                    "readinessProbe": {
                        "httpGet": {"path": "/health", "port": "http", "scheme": "HTTP"},
                        "periodSeconds": 5,
                        "timeoutSeconds": 1,
                        "failureThreshold": 3,
                        "successThreshold": 1,
                    },
                    "startupProbe": {
                        "httpGet": {"path": "/health", "port": "http", "scheme": "HTTP"},
                        "periodSeconds": 5,
                        "timeoutSeconds": 1,
                        "failureThreshold": 240,
                        "successThreshold": 1,
                    },
                    "resources": {
                        "requests": {
                            "cpu": "8",
                            "memory": "64Gi",
                            "ephemeral-storage": "4Gi",
                            spec["accelerator_resource_name"]: str(spec["accelerators_per_replica"]),
                        },
                        "limits": {
                            "cpu": "24",
                            "memory": "160Gi",
                            "ephemeral-storage": "32Gi",
                            spec["accelerator_resource_name"]: str(spec["accelerators_per_replica"]),
                        },
                    },
                    "securityContext": {
                        "allowPrivilegeEscalation": False,
                        "capabilities": {"drop": ["ALL"]},
                    },
                    "volumeMounts": [
                        {"name": "model", "mountPath": payload_mount, "readOnly": True},
                        {"name": "runtime-cache", "mountPath": "/runtime-cache"},
                        {"name": "tmp", "mountPath": "/tmp"},
                    ],
                }
            ],
            "volumes": [
                {"name": "model", "persistentVolumeClaim": {"claimName": spec["payload_claim_name"]}},
                {"name": "runtime-cache", "emptyDir": {"sizeLimit": "16Gi"}},
                {"name": "tmp", "emptyDir": {"sizeLimit": "8Gi"}},
            ],
        },
    }


def render_arm(
    spec: dict[str, Any],
    *,
    arm: str,
    attempt: int,
    campaign_id: str,
    warmup: bool = False,
) -> dict[str, Any]:
    """Render one attempt: its Pod, its Service, and any holder it needs.

    The candidate Pods are produced by mutating the conventional render with
    the production mechanism adapters, so the diff between arms is exactly the
    mechanism.
    """

    if arm not in ARMS:
        raise ArmError(f"unknown campaign arm {arm}")
    # A warm-up and the measured attempt of the same ordinal must not share a
    # name, or the second apply races the first Pod's deletion.
    ordinal = f"w{attempt:02d}" if warmup else f"{attempt:03d}"
    name = f"fsm-{campaign_id}-{arm.replace('host-memory-residency', 'hostmem')}-{ordinal}"[:63]
    labels = {
        CAMPAIGN_LABEL: campaign_id,
        ARM_LABEL: arm,
        ATTEMPT_LABEL: ordinal,
        "app.kubernetes.io/managed-by": "fs2-fast-start-mechanism-campaign",
    }
    pod = _base_pod(spec, name=name, labels=labels)
    pod_spec = pod["spec"]
    pod_metadata = pod["metadata"]
    declaration = declaration_for(spec, arm)
    holders: list[dict[str, Any]] = []

    if arm == "regional-cache":
        assert isinstance(declaration, RegionalCacheQualification)
        configure_regional_cache(
            pod_spec=pod_spec,
            pod_metadata=pod_metadata,
            qualification=declaration,
            runtime_image=spec["runtime_image"],
            runtime_container_name=spec["runtime_container_name"],
        )
    elif arm == "host-memory-residency":
        assert isinstance(declaration, HostMemoryResidencyQualification)
        configure_host_memory_residency(
            pod_spec=pod_spec,
            pod_metadata=pod_metadata,
            qualification=declaration,
            runtime_image=spec["runtime_image"],
            model_ref=spec["model_ref"],
            runtime_container_name=spec["runtime_container_name"],
        )
        holders = residency_holder_manifests(
            namespace=spec["namespace"],
            name=declaration.holder.name,
            model_ref=spec["model_ref"],
            qualification=declaration,
            image=spec["runtime_image"],
            node_selector={"kubernetes.io/hostname": spec["node_name"]},
            tolerations=pod_spec["tolerations"],
            labels={CAMPAIGN_LABEL: campaign_id, "app.kubernetes.io/managed-by": "fs2-fast-start-mechanism-campaign"},
            annotations={},
            pod_security_context=pod_spec["securityContext"],
        )
    elif arm == "host-memory-residency-sleep-offload":
        assert isinstance(declaration, HostMemoryResidencyQualification)
        configure_host_memory_residency(
            pod_spec=pod_spec,
            pod_metadata=pod_metadata,
            qualification=declaration,
            runtime_image=spec["runtime_image"],
            model_ref=spec["model_ref"],
            runtime_container_name=spec["runtime_container_name"],
        )
        container = pod_spec["containers"][0]
        # vLLM's sleep level 1 is the runtime-side RAM offload: the engine stays
        # alive with its weights in host memory and its GPU memory released.
        container["args"] = [*container["args"], "--enable-sleep-mode"]
    else:
        if arm == "gpu-resident":
            assert isinstance(declaration, GpuResidentQualification)
            configure_gpu_resident(
                pod_spec=pod_spec,
                pod_metadata=pod_metadata,
                qualification=declaration,
                configured_hot_replicas=1,
                role="standby",
                runtime_container_name=spec["runtime_container_name"],
            )

    service = {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": {"name": name, "namespace": spec["namespace"], "labels": dict(labels)},
        "spec": {
            "selector": {CAMPAIGN_LABEL: campaign_id, ARM_LABEL: arm, ATTEMPT_LABEL: ordinal},
            "ports": [{"name": "http", "port": spec["service_port"], "targetPort": "http", "protocol": "TCP"}],
        },
    }
    return {
        "name": name,
        "arm": arm,
        "attempt": attempt,
        "mechanism": mechanism_name(arm),
        "promotion": arm in PROMOTION_ARMS,
        "readiness_gate": GPU_RESIDENT_READINESS_GATE if arm == "gpu-resident" else None,
        "config_digest": None if declaration is None else declaration.config_digest,
        "mechanism_config_digest": expected_mechanism_config_digest(spec, arm),
        "residency_mode": getattr(declaration, "residency_mode", None),
        "reserved_host_memory_bytes": getattr(declaration, "reserved_bytes", None),
        # A parked replica holds its accelerator for as long as it waits, and a
        # sleeping engine is still a parked replica: sleep level 1 releases the
        # weights from device memory but the Pod keeps its accelerator. Record
        # that, so a promotion arm never looks free.
        "reserved_accelerators": (
            getattr(declaration, "reserved_accelerators", None)
            or (spec["accelerators_per_replica"] if arm in PROMOTION_ARMS else None)
        ),
        "pod": pod,
        "service": service,
        "holders": holders,
        "holder_selector": {HOST_MEMORY_RESIDENCY_LABEL: spec["model_ref"]} if holders else None,
    }


def render_all(spec: dict[str, Any], *, campaign_id: str = "preview") -> dict[str, dict[str, Any]]:
    return {arm: render_arm(spec, arm=arm, attempt=0, campaign_id=campaign_id) for arm in ARMS}


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", default="qwen3-8b")
    parser.add_argument("--arm", choices=ARMS)
    parser.add_argument("--campaign-id", default="preview")
    parser.add_argument("--node", default="h100-node-a", help="the node the arms would run on")
    parser.add_argument("--attempt", type=int, default=0)
    arguments = parser.parse_args(argv)
    spec = target(load_contract(), arguments.target)
    spec["node_name"] = arguments.node
    if arguments.arm:
        rendered: Any = render_arm(spec, arm=arguments.arm, attempt=arguments.attempt, campaign_id=arguments.campaign_id)
    else:
        rendered = render_all(spec, campaign_id=arguments.campaign_id)
    json.dump(copy.deepcopy(rendered), sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
