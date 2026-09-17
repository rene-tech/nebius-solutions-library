from __future__ import annotations

import hashlib
import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("inventory", ROOT / "scripts" / "audit_sai07_baseline_inventory.py")
assert SPEC and SPEC.loader
inventory = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(inventory)


def test_exact_frozen_scientific_namespace_inventory() -> None:
    assert inventory.SCIENTIFIC_NAMESPACES == (
        "fs2-academic-poc",
        "fs2-bioir-boltz2",
        "fs2-bioir-coverage",
        "fs2-bioir-openfold",
        "fs2-bioir-protenix",
        "fs2-bioir-snapshot",
    )


def test_jobset_replicated_job_templates_are_scanned() -> None:
    value = {
        "kind": "JobSet",
        "spec": {
            "replicatedJobs": [
                {
                    "template": {
                        "spec": {
                            "template": {
                                "metadata": {
                                    "annotations": {
                                        "container.apparmor.security.beta.kubernetes.io/runtime": "unconfined"
                                    }
                                },
                                "spec": {
                                    "containers": [
                                        {
                                            "name": "runtime",
                                            "securityContext": {
                                                "runAsUser": 0,
                                                "allowPrivilegeEscalation": True,
                                            },
                                        }
                                    ],
                                    "volumes": [{"name": "host", "hostPath": {"path": "/"}}],
                                },
                            }
                        }
                    }
                }
            ]
        },
    }
    templates = inventory.pod_templates(value, ())
    assert len(templates) == 1
    baseline, restricted = inventory.pod_findings(*templates[0])
    assert "hostPath" in baseline
    assert "unconfinedAppArmor" in baseline
    assert "root" in restricted


def test_baseline_scanner_rejects_every_retained_unsafe_shape() -> None:
    spec = {
        "hostNetwork": True,
        "hostPID": True,
        "hostIPC": True,
        "securityContext": {"runAsUser": 0},
        "volumes": [{"hostPath": {"path": "/mnt/fs2-reference-data/data"}}],
        "initContainers": [{"securityContext": {"privileged": True, "capabilities": {"add": ["SYS_ADMIN"]}}}],
        "containers": [
            {
                "ports": [{"hostPort": 8080}],
                "securityContext": {
                    "procMount": "Unmasked",
                    "seccompProfile": {"type": "Unconfined"},
                },
            }
        ],
    }
    assert inventory.baseline_findings(spec) == [
        "capabilities",
        "hostIPC",
        "hostNetwork",
        "hostPID",
        "hostPath",
        "hostPort",
        "privileged",
        "procMount",
        "unconfinedSeccomp",
    ]
    baseline, restricted = inventory.pod_findings(
        spec,
        {"container.apparmor.security.beta.kubernetes.io/runtime": "unconfined"},
    )
    assert "unconfinedAppArmor" in baseline
    assert {"root", "runAsNonRoot", "allowPrivilegeEscalation", "seccompProfile"}.issubset(restricted)


def test_reviewed_baseline_pod_has_no_findings() -> None:
    spec = {
        "automountServiceAccountToken": False,
        "securityContext": {"seccompProfile": {"type": "RuntimeDefault"}},
        "volumes": [{"persistentVolumeClaim": {"claimName": "model-cache"}}],
        "containers": [
            {
                "securityContext": {
                    "allowPrivilegeEscalation": False,
                    "capabilities": {"drop": ["ALL"]},
                }
            }
        ],
    }
    assert inventory.baseline_findings(spec) == []


def baseline_artifact(
    schema: str = inventory.SCHEMA,
    *,
    reference_host_paths: int = 80,
    baseline_incompatible_objects: int = 91,
    restricted_incompatible_objects: int = 151,
) -> dict[str, object]:
    collections = [
        {
            "api_version": api_version,
            "kind": kind,
            "namespace": namespace,
            "resource_version": "1",
            "item_count": 0,
        }
        for namespace in inventory.BASELINE_NAMESPACES
        for kind, (api_version, _, _) in inventory.COLLECTIONS.items()
        if schema == inventory.SCHEMA or kind != "ConfigMap"
    ]
    value: dict[str, object] = {
        "schema": schema,
        "captured_at": "2026-09-16T20:00:00Z",
        "cluster": {"kube_system_uid": "cluster-uid"},
        "scientific_namespaces": list(inventory.SCIENTIFIC_NAMESPACES),
        "inspected_namespaces": list(inventory.BASELINE_NAMESPACES),
        "collections": collections,
        "objects": [],
        "reference_host_paths": reference_host_paths,
        "baseline_incompatible_objects": baseline_incompatible_objects,
        "restricted_incompatible_objects": restricted_incompatible_objects,
        "legacy_controller_objects": [],
        "unauthorized_exception_objects": [],
    }
    value["inventory_sha256"] = hashlib.sha256(inventory.canonical(value)).hexdigest()
    return value


def test_v4_bootstrap_accepts_the_exact_current_authoritative_counts() -> None:
    artifact = baseline_artifact()
    payload = inventory.canonical(artifact)
    live = {**artifact, "captured_at": "2026-09-16T20:00:30Z"}
    result = inventory.verify_against_artifact(
        live,
        payload,
        hashlib.sha256(payload).hexdigest(),
        "initial",
    )
    assert result["baseline_reference_host_paths"] == 80
    assert result["baseline_restricted_incompatible_objects"] == 151

    drifted = {**live, "restricted_incompatible_objects": 150}
    with pytest.raises(inventory.InventoryError, match="differs"):
        inventory.verify_against_artifact(
            drifted,
            payload,
            hashlib.sha256(payload).hexdigest(),
            "initial",
        )


def test_inventory_covers_native_and_custom_workload_controllers() -> None:
    assert {
        "Pod",
        "ReplicationController",
        "ReplicaSet",
        "Deployment",
        "StatefulSet",
        "DaemonSet",
        "Job",
        "CronJob",
        "JobSet",
        "ModelDeployment",
        "ScaledObject",
        "PodTemplate",
        "ServiceAccount",
        "NetworkPolicy",
    }.issubset(inventory.COLLECTIONS)


def test_legacy_v3_is_rejected_even_when_historical_counts_match() -> None:
    artifact = baseline_artifact(
        "fs2-serve.nebius.ai/sai07-baseline-inventory/v3",
        reference_host_paths=103,
        baseline_incompatible_objects=103,
        restricted_incompatible_objects=716,
    )
    with pytest.raises(inventory.InventoryError, match="schema is unsupported"):
        inventory.validate_artifact(artifact)

    current = baseline_artifact()
    assert inventory.validate_artifact(current)["reference_host_paths"] == 80


def test_v4_projection_is_shared_with_cleanup_and_binds_deletion_timestamp() -> None:
    value = {
        "apiVersion": "apps/v1",
        "kind": "DaemonSet",
        "metadata": {
            "name": "legacy",
            "namespace": "fs2-models",
            "uid": "uid-1",
            "resourceVersion": "17",
            "generation": 2,
            "deletionTimestamp": None,
            "labels": {},
            "annotations": {},
            "ownerReferences": [],
        },
        "spec": {"selector": {"matchLabels": {"app": "legacy"}}},
    }
    assert inventory.live_projection(value)["metadata"]["deletionTimestamp"] is None


def test_frozen_artifact_refuses_an_omitted_namespace_controller_collection() -> None:
    artifact = baseline_artifact()
    artifact["collections"] = artifact["collections"][:-1]  # type: ignore[index]
    unsigned = dict(artifact)
    unsigned.pop("inventory_sha256")
    artifact["inventory_sha256"] = hashlib.sha256(inventory.canonical(unsigned)).hexdigest()
    with pytest.raises(inventory.InventoryError, match="omits"):
        inventory.validate_artifact(artifact)


def test_legacy_controller_inventory_excludes_terraform_profiles() -> None:
    metadata = {
        "name": "fs2-runtime-old",
        "uid": "uid-old",
        "resourceVersion": "17",
        "labels": {"app.kubernetes.io/part-of": "fs2-serve"},
    }
    assert inventory.legacy_controller_identity("fs2-models", "NetworkPolicy", metadata)
    metadata["name"] = "fs2-network-profile-mounted-content"
    metadata["labels"] = {
        "app.kubernetes.io/part-of": "fs2-serve",
        "app.kubernetes.io/managed-by": "terraform",
    }
    assert inventory.legacy_controller_identity("fs2-models", "NetworkPolicy", metadata) is None
