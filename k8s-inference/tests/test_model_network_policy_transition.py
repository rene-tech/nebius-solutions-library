from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "stages/workloads/scripts/model_network_policy_transition.py"
SPEC = importlib.util.spec_from_file_location("model_network_policy_transition", SCRIPT)
assert SPEC and SPEC.loader
transition = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(transition)

PROFILE = "gateway-zero-egress-tcp-8000-v1"
POLICY = f"fs2-runtime-profile-{PROFILE}"
ADMISSION_POLICY = "fs2-model-network-profile-apps"
ADMISSION_BINDING = f"{ADMISSION_POLICY}-fs2-models"
MARKER_ADMISSION_POLICY = "fs2-model-network-boundary-marker"
MARKER_ADMISSION_BINDING = f"{MARKER_ADMISSION_POLICY}-fs2-models"
CONTROLLER = "fs2-serve-control-plane-model-controller"
IMAGE = {
    "repository": "registry.example.test/fs2/control-plane",
    "digest": "sha256:" + "a" * 64,
}


def prepare_contract() -> dict[str, object]:
    profiles = [PROFILE]
    return {
        "phase": "inventory",
        "cluster_id": "mk8scluster-test",
        "namespace": "fs2-models",
        "profiles": profiles,
        "profiles_sha256": transition.hashlib.sha256(
            json.dumps(profiles, separators=(",", ":")).encode()
        ).hexdigest(),
        "allow_policy_names": [POLICY],
        "admission_policy_names": [MARKER_ADMISSION_POLICY, ADMISSION_POLICY],
        "admission_binding_names": [MARKER_ADMISSION_BINDING, ADMISSION_BINDING],
        "controller_deployment_name": CONTROLLER,
        "control_plane_image": IMAGE,
        "inventory_receipt_sha256": None,
    }


def deployment(*, name: str = "qwen3-8b", profile: str = PROFILE) -> dict[str, object]:
    labels = {
        "app.kubernetes.io/component": "model-runtime",
        "app.kubernetes.io/part-of": "fs2-serve",
        "fs2-serve.nebius.ai/network-profile": profile,
    }
    return {
        "metadata": {
            "name": name,
            "uid": f"uid-{name}",
            "generation": 2,
            "labels": labels,
        },
        "spec": {"replicas": 1, "template": {"metadata": {"labels": labels}}},
        "status": {
            "observedGeneration": 2,
            "updatedReplicas": 1,
            "readyReplicas": 1,
            "availableReplicas": 1,
            "unavailableReplicas": 0,
        },
    }


def workload_resources(*deployments: dict[str, object]) -> dict[str, object]:
    return {
        "deployments": {"items": list(deployments)},
        "statefulsets": {"items": []},
        "daemonsets": {"items": []},
        "replicasets": {"items": []},
        "jobs": {"items": []},
        "jobsets": {"api_available": False, "items": []},
    }


def pod(
    *, name: str = "qwen3-8b-pod", owner_uid: str = "uid-qwen3-8b"
) -> dict[str, object]:
    labels = {
        "app.kubernetes.io/part-of": "fs2-serve",
        "fs2-serve.nebius.ai/network-profile": PROFILE,
    }
    return {
        "metadata": {
            "name": name,
            "uid": f"uid-{name}",
            "labels": labels,
            "ownerReferences": [
                {"kind": "Deployment", "uid": owner_uid, "controller": True}
            ],
        },
        "status": {
            "phase": "Running",
            "conditions": [{"type": "Ready", "status": "True"}],
        },
    }


def controller_deployments() -> dict[str, object]:
    return {
        "items": [
            {
                "metadata": {
                    "name": CONTROLLER,
                    "uid": "uid-controller",
                    "generation": 4,
                },
                "spec": {
                    "replicas": 1,
                    "template": {
                        "metadata": {"labels": {}},
                        "spec": {
                            "containers": [
                                {
                                    "name": "model-controller",
                                    "image": f"{IMAGE['repository']}@{IMAGE['digest']}",
                                }
                            ]
                        },
                    },
                },
                "status": {
                    "observedGeneration": 4,
                    "updatedReplicas": 1,
                    "readyReplicas": 1,
                    "availableReplicas": 1,
                    "unavailableReplicas": 0,
                },
            }
        ]
    }


def controller_pods() -> dict[str, object]:
    return {
        "items": [
            {
                "metadata": {"name": "controller-pod", "uid": "uid-controller-pod"},
                "status": {
                    "phase": "Running",
                    "conditions": [{"type": "Ready", "status": "True"}],
                    "containerStatuses": [
                        {
                            "name": "model-controller",
                            "imageID": (
                                f"docker-pullable://{IMAGE['repository']}@{IMAGE['digest']}"
                            ),
                        }
                    ],
                },
            }
        ]
    }


def admission_bindings() -> dict[str, object]:
    return {
        "items": [
            {"metadata": {"name": ADMISSION_BINDING, "uid": "uid-binding"}},
            {
                "metadata": {
                    "name": MARKER_ADMISSION_BINDING,
                    "uid": "uid-marker-binding",
                }
            },
        ]
    }


def capture(
    contract: dict[str, object] | None = None,
    resources: dict[str, object] | None = None,
    pods: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    return transition.inventory_receipt(
        contract or prepare_contract(),
        resources or workload_resources(deployment()),
        {"items": [pod()] if pods is None else pods},
        controller_deployments(),
        controller_pods(),
        admission_bindings(),
        captured_at="2026-09-16T18:00:00Z",
    )


def test_inventory_receipt_binds_workload_pod_controller_and_admission() -> None:
    receipt = capture()

    assert list(receipt["workloads"]) == ["apps/v1/Deployment/qwen3-8b"]
    assert receipt["pods"]["qwen3-8b-pod"]["profile"] == PROFILE
    assert receipt["live_controller"]["deployment_uid"] == "uid-controller"
    assert receipt["admission_bindings"] == {
        ADMISSION_BINDING: "uid-binding",
        MARKER_ADMISSION_BINDING: "uid-marker-binding",
    }
    assert receipt["payload_sha256"] == transition._sha256(
        {key: value for key, value in receipt.items() if key != "payload_sha256"}
    )


@pytest.mark.parametrize(
    "mutate",
    [
        lambda item: item["metadata"]["labels"].pop(
            "fs2-serve.nebius.ai/network-profile"
        ),
        lambda item: item["spec"]["template"]["metadata"]["labels"].update(
            {"fs2-serve.nebius.ai/network-profile": "unknown"}
        ),
        lambda item: item["metadata"]["labels"].pop("app.kubernetes.io/part-of"),
    ],
)
def test_inventory_refuses_missing_mismatched_or_unknown_labels(mutate) -> None:
    item = deployment()
    mutate(item)
    with pytest.raises(transition.ReceiptError):
        capture(resources=workload_resources(item))


def test_inventory_refuses_empty_namespace() -> None:
    with pytest.raises(transition.ReceiptError, match="empty"):
        capture(resources=workload_resources(), pods=[])


def test_inventory_refuses_naked_or_orphaned_pods() -> None:
    naked = pod()
    naked["metadata"].pop("ownerReferences")
    with pytest.raises(transition.ReceiptError, match="naked"):
        capture(pods=[naked])

    with pytest.raises(transition.ReceiptError, match="outside the complete"):
        capture(pods=[pod(owner_uid="unknown")])


def test_inventory_refuses_unconverged_rollout() -> None:
    item = deployment()
    item["status"]["readyReplicas"] = 0
    with pytest.raises(transition.ReceiptError, match="not converged"):
        capture(resources=workload_resources(item))


def test_inventory_covers_old_replicasets_statefulsets_daemonsets_jobs_and_jobsets() -> (
    None
):
    labels = {
        "app.kubernetes.io/part-of": "fs2-serve",
        "fs2-serve.nebius.ai/network-profile": PROFILE,
    }

    def item(
        kind: str, name: str, spec: dict[str, object], status: dict[str, object]
    ) -> dict[str, object]:
        return {
            "metadata": {
                "name": name,
                "uid": f"uid-{name}",
                "generation": 1,
                "labels": labels,
            },
            "spec": spec,
            "status": status,
        }

    template = {"metadata": {"labels": labels}, "spec": {}}
    resources = workload_resources(deployment())
    resources["statefulsets"]["items"] = [
        item(
            "StatefulSet",
            "stateful",
            {"replicas": 0, "template": template},
            {
                "observedGeneration": 1,
                "updatedReplicas": 0,
                "readyReplicas": 0,
                "currentReplicas": 0,
                "currentRevision": "r1",
                "updateRevision": "r1",
            },
        )
    ]
    resources["daemonsets"]["items"] = [
        item(
            "DaemonSet",
            "keeper",
            {"template": template},
            {
                "observedGeneration": 1,
                "desiredNumberScheduled": 0,
                "updatedNumberScheduled": 0,
                "numberReady": 0,
                "numberAvailable": 0,
                "numberUnavailable": 0,
            },
        )
    ]
    resources["replicasets"]["items"] = [
        item(
            "ReplicaSet",
            "old-revision",
            {"replicas": 0, "template": template},
            {"observedGeneration": 1, "readyReplicas": 0, "availableReplicas": 0},
        )
    ]
    resources["jobs"]["items"] = [
        item("Job", "evaluation", {"suspend": True, "template": template}, {})
    ]
    resources["jobsets"] = {
        "api_available": True,
        "items": [
            item(
                "JobSet",
                "scientific",
                {
                    "replicatedJobs": [
                        {
                            "name": "gang",
                            "template": {
                                "metadata": {"labels": labels},
                                "spec": {"template": template},
                            },
                        }
                    ]
                },
                {},
            )
        ],
    }

    workloads, apis = transition._workload_inventory(prepare_contract(), resources)
    assert len(workloads) == 6
    assert "apps/v1/ReplicaSet/old-revision" in workloads
    assert "jobset.x-k8s.io/v1alpha2/JobSet/scientific" in workloads
    assert apis["jobset.x-k8s.io/v1alpha2/JobSet"] is True


def test_inventory_can_refresh_in_enforced_phase_and_apply_verifier_detects_change() -> (
    None
):
    contract = prepare_contract()
    contract["phase"] = "enforce"
    receipt = capture(contract=contract)
    result = transition.verify_enforce(
        contract,
        receipt,
        workload_resources(deployment()),
        {"items": [pod()]},
        controller_deployments(),
        controller_pods(),
        admission_bindings(),
    )
    assert result["status"] == "verified"

    changed = deployment()
    changed["metadata"]["uid"] = "replacement-uid"
    with pytest.raises(transition.ReceiptError, match="apply-time"):
        transition.verify_enforce(
            contract,
            receipt,
            workload_resources(changed),
            {"items": [pod(owner_uid="replacement-uid")]},
            controller_deployments(),
            controller_pods(),
            admission_bindings(),
        )


def test_terraform_enforcement_orders_apply_fence_before_default_deny() -> None:
    source = (ROOT / "stages/workloads/network_policies.tf").read_text()
    default_deny = source.split(
        'resource "kubernetes_network_policy_v1" "model_namespace_default_deny"', 1
    )[1]
    assert "terraform_data.model_runtime_network_policy_apply_fence" in default_deny
    assert 'provisioner "local-exec"' in source
    assert "verify-enforce" in source
    assert 'kind       = "ValidatingAdmissionPolicy"' in source
    assert 'kind       = "ValidatingAdmissionPolicyBinding"' in source
    assert "immutable = true" in source
    assert (
        'for_each = var.model_runtime_network_policy.phase == "prepare" ? {} : '
        "local.model_runtime_admission_specs" in source
    )
    assert (
        'count = var.model_runtime_network_policy.phase == "prepare" ? 0 : 1' in source
    )
    binding = source.split(
        'resource "kubernetes_manifest" "model_runtime_network_profile_admission_binding"',
        1,
    )[1].split(
        'resource "kubernetes_config_map_v1" "model_runtime_network_enforcement"',
        1,
    )[0]
    for dependency in (
        "helm_release.control_plane",
        "kubernetes_manifest.model",
        "kubernetes_manifest.cold_start_keeper",
        "kubernetes_manifest.kueue_admission_acceptance",
    ):
        assert dependency in binding
    assert "prevent_destroy = true" in binding
    marker_admission = source.split(
        'resource "kubernetes_manifest" "model_runtime_network_boundary_marker_admission"',
        1,
    )[1].split(
        'resource "kubernetes_manifest" "model_runtime_network_boundary_marker_admission_binding"',
        1,
    )[0]
    assert 'operations  = ["UPDATE", "DELETE"]' in marker_admission
    assert (
        "oldObject.metadata.name != 'fs2-runtime-network-policy-boundary-v2'"
        in marker_admission
    )
    assert "prevent_destroy = true" in marker_admission


def rollback_contract() -> dict[str, object]:
    contract = prepare_contract()
    contract.update(
        {
            "phase": "rollback-remove-deny",
            "inventory_receipt_sha256": "b" * 64,
        }
    )
    return contract


def test_deny_absent_receipt_requires_allow_profiles_and_no_deny() -> None:
    receipt = transition.deny_absent_receipt(
        rollback_contract(),
        {"items": [{"metadata": {"name": POLICY}}]},
        captured_at="2026-09-16T18:05:00Z",
    )

    assert receipt["default_deny_absent"] is True
    assert receipt["allow_policy_names"] == [POLICY]
    assert receipt["enforcement_payload_sha256"] == "b" * 64


@pytest.mark.parametrize(
    "items, message",
    [
        ([{"metadata": {"name": "default-deny"}}], "still present"),
        ([], "missing"),
    ],
)
def test_deny_absent_receipt_refuses_unsafe_rollback(items, message) -> None:
    with pytest.raises(transition.ReceiptError, match=message):
        transition.deny_absent_receipt(
            rollback_contract(),
            {"items": items},
            captured_at="2026-09-16T18:05:00Z",
        )
