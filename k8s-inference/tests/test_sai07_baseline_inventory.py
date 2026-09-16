from __future__ import annotations

import importlib.util
from pathlib import Path

import hashlib
import json
import pytest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "inventory", ROOT / "scripts" / "audit_sai07_baseline_inventory.py"
)
assert SPEC and SPEC.loader
inventory = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(inventory)


def test_exact_frozen_scientific_namespace_inventory() -> None:
    assert inventory.SCIENTIFIC_NAMESPACES == (
        "fs2-bioir-boltz2",
        "fs2-bioir-coverage",
        "fs2-bioir-openfold",
        "fs2-bioir-protenix",
        "fs2-bioir-snapshot",
    )


def test_baseline_scanner_rejects_every_retained_unsafe_shape() -> None:
    spec = {
        "hostNetwork": True,
        "hostPID": True,
        "hostIPC": True,
        "securityContext": {"runAsUser": 0},
        "volumes": [{"hostPath": {"path": "/mnt/fs2-reference-data/data"}}],
        "initContainers": [
            {"securityContext": {"privileged": True, "capabilities": {"add": ["SYS_ADMIN"]}}}
        ],
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
    assert {"root", "runAsNonRoot", "allowPrivilegeEscalation", "seccompProfile"}.issubset(
        restricted
    )


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


def baseline_artifact() -> dict[str, object]:
    value: dict[str, object] = {
        "schema": inventory.SCHEMA,
        "captured_at": "2026-09-16T20:00:00Z",
        "cluster": {"kube_system_uid": "cluster-uid"},
        "scientific_namespaces": list(inventory.SCIENTIFIC_NAMESPACES),
        "inspected_namespaces": list(inventory.BASELINE_NAMESPACES),
        "collections": [],
        "objects": [],
        "reference_host_paths": 103,
        "baseline_incompatible_objects": 103,
        "restricted_incompatible_objects": 716,
        "unauthorized_exception_objects": [],
    }
    value["inventory_sha256"] = hashlib.sha256(inventory.canonical(value)).hexdigest()
    return value


def test_frozen_artifact_proves_the_exact_initial_103_716_counts() -> None:
    artifact = baseline_artifact()
    payload = inventory.canonical(artifact)
    live = {**artifact, "captured_at": "2026-09-16T20:00:30Z"}
    result = inventory.verify_against_artifact(
        live,
        payload,
        hashlib.sha256(payload).hexdigest(),
        "initial",
    )
    assert result["baseline_reference_host_paths"] == 103
    assert result["baseline_restricted_incompatible_objects"] == 716

    drifted = {**live, "restricted_incompatible_objects": 715}
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
    }.issubset(inventory.COLLECTIONS)
