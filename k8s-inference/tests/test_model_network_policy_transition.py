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
FREEZE_ADMISSION_POLICY = "fs2-model-network-controller-freeze"
FREEZE_ADMISSION_BINDING = f"{FREEZE_ADMISSION_POLICY}-fs2-system"
HELM_FREEZE_ADMISSION_POLICY = "fs2-model-network-helm-freeze"
HELM_FREEZE_ADMISSION_BINDING = f"{HELM_FREEZE_ADMISSION_POLICY}-fs2-system"
TRANSITION_WRITER = "security-remediation@example.test"
CONTROLLER = "fs2-serve-control-plane-model-controller"
IMAGE = {
    "repository": "registry.example.test/fs2/control-plane",
    "digest": "sha256:" + "a" * 64,
}


def prepare_contract() -> dict[str, object]:
    profiles = [PROFILE]
    policies = admission_policies()["items"]
    bindings = admission_bindings()["items"]
    return {
        "phase": "inventory",
        "cluster_id": "mk8scluster-test",
        "namespace": "fs2-models",
        "profiles": profiles,
        "serving_profiles": profiles,
        "profiles_sha256": transition.hashlib.sha256(
            json.dumps(profiles, separators=(",", ":")).encode()
        ).hexdigest(),
        "allow_policy_names": [POLICY],
        "admission_policy_names": sorted(
            [
                MARKER_ADMISSION_POLICY,
                FREEZE_ADMISSION_POLICY,
                HELM_FREEZE_ADMISSION_POLICY,
                ADMISSION_POLICY,
            ]
        ),
        "admission_binding_names": sorted(
            [
                MARKER_ADMISSION_BINDING,
                FREEZE_ADMISSION_BINDING,
                HELM_FREEZE_ADMISSION_BINDING,
                ADMISSION_BINDING,
            ]
        ),
        "admission_policy_spec_sha256": {
            item["metadata"]["name"]: transition._sha256(item["spec"])
            for item in policies
        },
        "admission_binding_spec_sha256": {
            item["metadata"]["name"]: transition._sha256(item["spec"])
            for item in bindings
        },
        "controller_deployment_name": CONTROLLER,
        "transition_lock_name": "fs2-model-network-transition",
        "transition_lock_namespace": "fs2-system",
        "transition_writer_username": TRANSITION_WRITER,
        "control_plane_image": IMAGE,
        "inventory_receipt_sha256": None,
    }


def deployment(*, name: str = "qwen3-8b", profile: str = PROFILE) -> dict[str, object]:
    labels = {
        "app.kubernetes.io/component": "model-runtime",
        "app.kubernetes.io/part-of": "fs2-serve",
        "fs2-serve.nebius.ai/network-workload-class": "runtime",
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
        "replicationcontrollers": {"items": []},
        "jobs": {"items": []},
        "cronjobs": {"items": []},
        "jobsets": {"api_available": False, "items": []},
    }


def pod(
    *, name: str = "qwen3-8b-pod", owner_uid: str = "uid-qwen3-8b"
) -> dict[str, object]:
    labels = {
        "app.kubernetes.io/component": "model-runtime",
        "app.kubernetes.io/part-of": "fs2-serve",
        "fs2-serve.nebius.ai/network-workload-class": "runtime",
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


def _policy(
    name: str,
    *,
    api_groups: list[str],
    operations: list[str],
    resources: list[str],
    expression: str,
) -> dict[str, object]:
    return {
        "metadata": {
            "name": name,
            "uid": f"uid-{name}",
            "resourceVersion": f"rv-{name}",
        },
        "spec": {
            "failurePolicy": "Fail",
            "matchConstraints": {
                "resourceRules": [
                    {
                        "apiGroups": api_groups,
                        "apiVersions": ["v1"],
                        "operations": operations,
                        "resources": resources,
                        "scope": "Namespaced",
                    }
                ]
            },
            "validations": [
                {
                    "expression": expression,
                    "message": "test admission fence",
                    "reason": "Forbidden",
                }
            ],
        },
    }


def admission_policies() -> dict[str, object]:
    return {
        "items": [
            _policy(
                ADMISSION_POLICY,
                api_groups=["apps"],
                operations=["CREATE", "UPDATE"],
                resources=["deployments"],
                expression="true",
            ),
            _policy(
                MARKER_ADMISSION_POLICY,
                api_groups=[""],
                operations=["UPDATE", "DELETE"],
                resources=["configmaps"],
                expression=(
                    "oldObject.metadata.name != "
                    "'fs2-runtime-network-policy-boundary-v2'"
                ),
            ),
            _policy(
                FREEZE_ADMISSION_POLICY,
                api_groups=["apps"],
                operations=["UPDATE", "DELETE"],
                resources=["deployments"],
                expression=f"oldObject.metadata.name != '{CONTROLLER}'",
            ),
            _policy(
                HELM_FREEZE_ADMISSION_POLICY,
                api_groups=[""],
                operations=["CREATE", "UPDATE", "DELETE"],
                resources=["configmaps", "secrets"],
                expression="true",
            ),
        ]
    }


def admission_bindings() -> dict[str, object]:
    def binding(name: str, policy: str, namespace: str) -> dict[str, object]:
        return {
            "metadata": {
                "name": name,
                "uid": f"uid-{name}",
                "resourceVersion": f"rv-{name}",
            },
            "spec": {
                "policyName": policy,
                "validationActions": ["Deny"],
                "matchResources": {
                    "namespaceSelector": {
                        "matchLabels": {
                            "kubernetes.io/metadata.name": namespace,
                        }
                    }
                },
            },
        }

    return {
        "items": [
            binding(ADMISSION_BINDING, ADMISSION_POLICY, "fs2-models"),
            binding(MARKER_ADMISSION_BINDING, MARKER_ADMISSION_POLICY, "fs2-models"),
            binding(FREEZE_ADMISSION_BINDING, FREEZE_ADMISSION_POLICY, "fs2-system"),
            binding(
                HELM_FREEZE_ADMISSION_BINDING,
                HELM_FREEZE_ADMISSION_POLICY,
                "fs2-system",
            ),
        ]
    }


def transition_leases(*, holder: str = "") -> dict[str, object]:
    return {
        "items": [
            {
                "metadata": {
                    "name": "fs2-model-network-transition",
                    "namespace": "fs2-system",
                    "uid": "uid-transition-lock",
                    "annotations": {
                        "fs2-serve.nebius.ai/network-transition-writer": TRANSITION_WRITER,
                    },
                },
                "spec": {
                    "holderIdentity": holder,
                    "leaseDurationSeconds": 7200 if holder else 1,
                    "renewTime": "2099-09-16T18:00:00Z",
                    "leaseTransitions": 1,
                },
            }
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
        admission_policies(),
        admission_bindings(),
        transition_leases(),
        captured_at="2026-09-16T18:00:00Z",
    )


def test_inventory_receipt_binds_workload_pod_controller_and_admission() -> None:
    receipt = capture()

    assert list(receipt["workloads"]) == ["apps/v1/Deployment/qwen3-8b"]
    assert receipt["pods"]["qwen3-8b-pod"]["profile"] == PROFILE
    assert receipt["pods"]["qwen3-8b-pod"]["workload_class"] == "runtime"
    assert receipt["live_controller"]["deployment_uid"] == "uid-controller"
    assert receipt["transition_lock_uid"] == "uid-transition-lock"
    assert receipt["schema"].endswith("/v4")
    assert (
        receipt["admission_policies"][ADMISSION_POLICY]["resource_version"]
        == f"rv-{ADMISSION_POLICY}"
    )
    assert (
        receipt["admission_bindings"][FREEZE_ADMISSION_BINDING]["resource_version"]
        == f"rv-{FREEZE_ADMISSION_BINDING}"
    )
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


def test_inventory_covers_every_pod_producing_controller_kind() -> None:
    runtime_labels = {
        "app.kubernetes.io/component": "model-runtime",
        "app.kubernetes.io/part-of": "fs2-serve",
        "fs2-serve.nebius.ai/network-workload-class": "runtime",
        "fs2-serve.nebius.ai/network-profile": PROFILE,
    }
    keeper_labels = {
        "app.kubernetes.io/component": "model-cache-keeper",
        "app.kubernetes.io/part-of": "fs2-serve",
        "fs2-serve.nebius.ai/network-workload-class": "cache-keeper",
        "fs2-serve.nebius.ai/network-profile": "cache-resident-zero-egress-v1",
    }
    job_labels = {
        "app.kubernetes.io/part-of": "fs2-serve",
        "fs2-serve.nebius.ai/job-kind": "batch",
        "fs2-serve.nebius.ai/network-workload-class": "internal-job",
        "fs2-serve.nebius.ai/network-profile": "job-internal-v1",
    }

    def item(
        name: str,
        labels: dict[str, str],
        spec: dict[str, object],
        status: dict[str, object],
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

    runtime_template = {"metadata": {"labels": runtime_labels}, "spec": {}}
    keeper_template = {"metadata": {"labels": keeper_labels}, "spec": {}}
    job_template = {"metadata": {"labels": job_labels}, "spec": {}}
    resources = workload_resources(deployment())
    resources["statefulsets"]["items"] = [
        item(
            "stateful",
            runtime_labels,
            {"replicas": 0, "template": runtime_template},
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
            "keeper",
            keeper_labels,
            {"template": keeper_template},
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
            "old-revision",
            runtime_labels,
            {"replicas": 0, "template": runtime_template},
            {"observedGeneration": 1, "readyReplicas": 0, "availableReplicas": 0},
        )
    ]
    resources["replicationcontrollers"]["items"] = [
        item(
            "legacy-controller",
            runtime_labels,
            {"replicas": 0, "template": runtime_template},
            {"observedGeneration": 1, "readyReplicas": 0, "availableReplicas": 0},
        )
    ]
    resources["jobs"]["items"] = [
        item("evaluation", job_labels, {"suspend": True, "template": job_template}, {})
    ]
    resources["cronjobs"]["items"] = [
        item(
            "scheduled-evaluation",
            job_labels,
            {
                "jobTemplate": {
                    "metadata": {"labels": job_labels},
                    "spec": {"template": job_template},
                }
            },
            {},
        )
    ]
    resources["jobsets"] = {
        "api_available": True,
        "items": [
            item(
                "scientific",
                job_labels,
                {
                    "replicatedJobs": [
                        {
                            "name": "gang",
                            "template": {
                                "metadata": {"labels": job_labels},
                                "spec": {"template": job_template},
                            },
                        }
                    ]
                },
                {},
            )
        ],
    }

    contract = prepare_contract()
    profiles = sorted([PROFILE, "cache-resident-zero-egress-v1", "job-internal-v1"])
    contract["profiles"] = profiles
    contract["profiles_sha256"] = transition.hashlib.sha256(
        json.dumps(profiles, separators=(",", ":")).encode()
    ).hexdigest()
    contract["allow_policy_names"] = [
        f"fs2-runtime-profile-{profile}" for profile in profiles
    ]
    workloads, apis = transition._workload_inventory(contract, resources)
    assert len(workloads) == 8
    assert "apps/v1/ReplicaSet/old-revision" in workloads
    assert "v1/ReplicationController/legacy-controller" in workloads
    assert "batch/v1/CronJob/scheduled-evaluation" in workloads
    assert "jobset.x-k8s.io/v1alpha2/JobSet/scientific" in workloads
    assert apis["jobset.x-k8s.io/v1alpha2/JobSet"] is True


def test_inventory_can_refresh_in_enforced_phase_and_apply_verifier_detects_change(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    contract = prepare_contract()
    contract["phase"] = "enforce"
    receipt = capture(contract=contract)
    monkeypatch.setenv("FS2_NETWORK_TRANSITION_LOCK_IDENTITY", "test-holder")
    result = transition.verify_enforce(
        contract,
        receipt,
        workload_resources(deployment()),
        {"items": [pod()]},
        controller_deployments(),
        controller_pods(),
        admission_policies(),
        admission_bindings(),
        transition_leases(holder="test-holder"),
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
            admission_policies(),
            admission_bindings(),
            transition_leases(holder="test-holder"),
        )


def test_runtime_workload_cannot_select_public_acquisition_profile() -> None:
    contract = prepare_contract()
    contract["profiles"] = sorted([PROFILE, "job-public-acquisition-v1"])
    item = deployment(profile="job-public-acquisition-v1")

    with pytest.raises(transition.ReceiptError, match="not authorized"):
        transition._authorized_profile(
            contract,
            "Deployment",
            item["metadata"],
            "untrusted runtime",
        )


def test_receipt_rejects_admission_spec_or_transition_lock_drift() -> None:
    policies = admission_policies()
    policies["items"][0]["spec"]["validations"][0]["expression"] = "false"
    with pytest.raises(transition.ReceiptError, match="differs from Terraform"):
        transition.inventory_receipt(
            prepare_contract(),
            workload_resources(deployment()),
            {"items": [pod()]},
            controller_deployments(),
            controller_pods(),
            policies,
            admission_bindings(),
            transition_leases(),
            captured_at="2026-09-16T18:00:00Z",
        )

    bindings = admission_bindings()
    bindings["items"][0]["spec"]["matchResources"]["objectSelector"] = {
        "matchLabels": {"unreviewed": "true"}
    }
    with pytest.raises(transition.ReceiptError, match="differs from Terraform"):
        transition.inventory_receipt(
            prepare_contract(),
            workload_resources(deployment()),
            {"items": [pod()]},
            controller_deployments(),
            controller_pods(),
            admission_policies(),
            bindings,
            transition_leases(),
            captured_at="2026-09-16T18:00:00Z",
        )

    with pytest.raises(transition.ReceiptError, match="active"):
        transition.inventory_receipt(
            prepare_contract(),
            workload_resources(deployment()),
            {"items": [pod()]},
            controller_deployments(),
            controller_pods(),
            admission_policies(),
            admission_bindings(),
            transition_leases(holder="another-transition"),
            captured_at="2026-09-16T18:00:00Z",
        )

    leases = transition_leases()
    leases["items"][0]["metadata"]["annotations"][
        "fs2-serve.nebius.ai/network-transition-writer"
    ] = "unrelated-writer@example.test"
    with pytest.raises(transition.ReceiptError, match="authenticated writer"):
        transition.inventory_receipt(
            prepare_contract(),
            workload_resources(deployment()),
            {"items": [pod()]},
            controller_deployments(),
            controller_pods(),
            admission_policies(),
            admission_bindings(),
            leases,
            captured_at="2026-09-16T18:00:00Z",
        )


def test_receipt_hashes_every_policy_and_binding_semantic_field() -> None:
    policy_mutations = (
        lambda spec: spec.update(
            {"paramKind": {"apiVersion": "v1", "kind": "ConfigMap"}}
        ),
        lambda spec: spec.update(
            {"matchConditions": [{"name": "drift", "expression": "true"}]}
        ),
        lambda spec: spec.update(
            {"auditAnnotations": [{"key": "drift", "valueExpression": "'x'"}]}
        ),
        lambda spec: spec.update(
            {"variables": [{"name": "drift", "expression": "true"}]}
        ),
    )
    binding_mutations = (
        lambda spec: spec["matchResources"].update(
            {"objectSelector": {"matchLabels": {"drift": "true"}}}
        ),
        lambda spec: spec["matchResources"].update(
            {
                "resourceRules": [
                    {
                        "apiGroups": ["*"],
                        "apiVersions": ["*"],
                        "operations": ["*"],
                        "resources": ["*"],
                    }
                ]
            }
        ),
        lambda spec: spec.update(
            {
                "paramRef": {
                    "name": "drift",
                    "namespace": "fs2-system",
                    "parameterNotFoundAction": "Allow",
                }
            }
        ),
    )

    for mutate in policy_mutations:
        policies = admission_policies()
        mutate(policies["items"][0]["spec"])
        with pytest.raises(transition.ReceiptError, match="differs from Terraform"):
            transition.inventory_receipt(
                prepare_contract(),
                workload_resources(deployment()),
                {"items": [pod()]},
                controller_deployments(),
                controller_pods(),
                policies,
                admission_bindings(),
                transition_leases(),
                captured_at="2026-09-16T18:00:00Z",
            )

    for mutate in binding_mutations:
        bindings = admission_bindings()
        mutate(bindings["items"][0]["spec"])
        with pytest.raises(transition.ReceiptError, match="differs from Terraform"):
            transition.inventory_receipt(
                prepare_contract(),
                workload_resources(deployment()),
                {"items": [pod()]},
                controller_deployments(),
                controller_pods(),
                admission_policies(),
                bindings,
                transition_leases(),
                captured_at="2026-09-16T18:00:00Z",
            )


def test_apply_verifier_binds_admission_resource_versions_and_all_semantics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    contract = prepare_contract()
    contract["phase"] = "enforce"
    receipt = capture(contract=contract)
    monkeypatch.setenv("FS2_NETWORK_TRANSITION_LOCK_IDENTITY", "test-holder")

    policies = admission_policies()
    policies["items"][0]["metadata"]["resourceVersion"] = "rv-replaced"
    with pytest.raises(transition.ReceiptError, match="apply-time"):
        transition.verify_enforce(
            contract,
            receipt,
            workload_resources(deployment()),
            {"items": [pod()]},
            controller_deployments(),
            controller_pods(),
            policies,
            admission_bindings(),
            transition_leases(holder="test-holder"),
        )

    policies = admission_policies()
    policies["items"][0]["spec"]["matchConditions"] = [
        {"name": "unreviewed", "expression": "true"}
    ]
    with pytest.raises(transition.ReceiptError, match="differs from Terraform"):
        transition.verify_enforce(
            contract,
            receipt,
            workload_resources(deployment()),
            {"items": [pod()]},
            controller_deployments(),
            controller_pods(),
            policies,
            admission_bindings(),
            transition_leases(holder="test-holder"),
        )


def test_source_authorizes_profile_creation_by_exact_writer_and_active_lease() -> None:
    source = (ROOT / "stages/workloads/network_policies.tf").read_text()
    assert "request.userInfo.username" in source
    assert "model_runtime_controller_writer" in source
    assert "model_runtime_scientific_writer" in source
    assert "model_runtime_jobset_writer" in source
    assert "model_runtime_active_transition_writer_expression" in source
    assert "paramKind = {" in source
    assert 'kind       = "Lease"' in source
    assert 'parameterNotFoundAction = "Deny"' in source
    assert "model_runtime_network_transition_guard_admission_binding" in source
    assert "model_runtime_network_lease_guard_admission_binding" in source
    assert "request.resource.resource == 'networkpolicies'" in source
    assert "request.namespace == 'fs2-models'" in source
    assert source.count("request.operation != 'CREATE' ||") >= 6
    assert (
        "resource_version"
        in (
            ROOT / "stages/workloads/scripts/model_network_policy_transition.py"
        ).read_text()
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
    assert "spec = local.model_runtime_admission_policy_specs[" in marker_admission
    assert (
        "oldObject.metadata.name != 'fs2-runtime-network-policy-boundary-v2'" in source
    )
    assert "prevent_destroy = true" in marker_admission
    freeze_binding = source.split(
        'resource "kubernetes_manifest" "model_runtime_network_controller_freeze_admission_binding"',
        1,
    )[1].split(
        'resource "kubernetes_config_map_v1" "model_runtime_network_enforcement"',
        1,
    )[0]
    assert '"rollback-remove-deny"' in freeze_binding
    assert '"rollback-helm"' not in freeze_binding
    assert "model_runtime_admission_binding_specs" in freeze_binding
    helm_freeze_admission = source.split(
        'resource "kubernetes_manifest" "model_runtime_network_helm_freeze_admission"',
        1,
    )[1].split(
        'resource "kubernetes_manifest" "model_runtime_network_controller_freeze_admission_binding"',
        1,
    )[0]
    assert "spec = local.model_runtime_admission_policy_specs[" in helm_freeze_admission
    assert 'resources   = ["configmaps", "secrets"]' in source
    assert "object.metadata.labels['owner'] != 'helm'" in source
    assert "object.metadata.labels['name'] != 'fs2-serve-control-plane'" in source
    helm_freeze_binding = source.split(
        'resource "kubernetes_manifest" "model_runtime_network_helm_freeze_admission_binding"',
        1,
    )[1].split(
        'resource "kubernetes_config_map_v1" "model_runtime_network_enforcement"',
        1,
    )[0]
    assert '"rollback-remove-deny"' in helm_freeze_binding
    assert '"rollback-helm"' not in helm_freeze_binding
    assert "model_runtime_admission_binding_specs" in helm_freeze_binding
    assert "model_runtime_network_class_label" in source
    assert "workload_classes" in source


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
