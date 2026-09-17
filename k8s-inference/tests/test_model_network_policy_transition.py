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
TRANSITION_WRITER = "fs2-model-network-transition"
LOCK_HOLDER = "testrun:123:0123456789abcdef0123456789abcdef"
CONTROLLER = "fs2-serve-control-plane-model-controller"
IMAGE = {
    "repository": "registry.example.test/fs2/control-plane",
    "digest": "sha256:" + "a" * 64,
}
RELEASE_INVENTORY = [
    {
        "group": "apps",
        "resource": "deployments",
        "namespace": "fs2-system",
        "name": "fs2-serve-control-plane-api",
        "operations": ["CREATE", "UPDATE"],
        "objectSha256": "5" * 64,
    }
]


def secure_pod_spec() -> dict[str, object]:
    return {
        "automountServiceAccountToken": False,
        "securityContext": {
            "runAsNonRoot": True,
            "seccompProfile": {"type": "RuntimeDefault"},
        },
        "containers": [
            {
                "name": "runtime",
                "image": "registry.example.test/fs2/runtime@sha256:" + "c" * 64,
                "securityContext": {
                    "allowPrivilegeEscalation": False,
                    "privileged": False,
                    "capabilities": {"drop": ["ALL"]},
                },
            }
        ],
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
        "boundary_webhook_name": "fs2-model-network-boundary",
        "boundary_authority": boundary_authority(),
        "provider_trust_root_sha256": "f" * 64,
        "signature_verifier_sha256": "e" * 64,
        "jobset_writer_username": (
            "system:serviceaccount:jobset-system:fs2-testrun-jobset-controller"
        ),
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
        "spec": {
            "replicas": 1,
            "template": {
                "metadata": {"labels": labels},
                "spec": secure_pod_spec(),
            },
        },
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
        "spec": secure_pod_spec(),
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
                        "fs2-serve.nebius.ai/network-transition-holder": holder,
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


def boundary_webhooks() -> dict[str, object]:
    common = {
        "admissionReviewVersions": ["v1"],
        "sideEffects": "None",
        "failurePolicy": "Fail",
        "matchPolicy": "Equivalent",
        "timeoutSeconds": 3,
        "clientConfig": {
            "caBundle": "dGVzdC1jYQ==",
            "service": {
                "name": "fs2-model-network-boundary",
                "namespace": "fs2-network-security",
                "path": "/validate",
                "port": 443,
            },
        },
    }
    return {
        "items": [
            {
                "metadata": {
                    "name": "fs2-model-network-boundary",
                    "uid": "uid-boundary-webhook",
                    "resourceVersion": "rv-boundary-webhook",
                },
                "webhooks": [
                    {
                        **common,
                        "name": "children.network.fs2.nebius.ai",
                        "namespaceSelector": {
                            "matchLabels": {"kubernetes.io/metadata.name": "fs2-models"}
                        },
                        "objectSelector": {
                            "matchExpressions": [
                                {
                                    "key": "fs2-serve.nebius.ai/network-profile",
                                    "operator": "Exists",
                                }
                            ]
                        },
                        "rules": [
                            {
                                "apiGroups": [""],
                                "apiVersions": ["v1"],
                                "operations": ["CREATE", "UPDATE"],
                                "resources": [
                                    "pods",
                                    "pods/ephemeralcontainers",
                                    "replicationcontrollers",
                                ],
                                "scope": "Namespaced",
                            },
                            {
                                "apiGroups": ["apps"],
                                "apiVersions": ["v1"],
                                "operations": ["CREATE", "UPDATE"],
                                "resources": [
                                    "deployments",
                                    "statefulsets",
                                    "daemonsets",
                                    "replicasets",
                                ],
                                "scope": "Namespaced",
                            },
                            {
                                "apiGroups": ["batch"],
                                "apiVersions": ["v1"],
                                "operations": ["CREATE", "UPDATE"],
                                "resources": ["jobs", "cronjobs"],
                                "scope": "Namespaced",
                            },
                            {
                                "apiGroups": ["jobset.x-k8s.io"],
                                "apiVersions": ["v1alpha2"],
                                "operations": ["CREATE", "UPDATE"],
                                "resources": ["jobsets"],
                                "scope": "Namespaced",
                            },
                        ],
                    },
                    {
                        **common,
                        "name": "models.network.fs2.nebius.ai",
                        "namespaceSelector": {
                            "matchLabels": {"kubernetes.io/metadata.name": "fs2-models"}
                        },
                        "rules": [
                            {
                                "apiGroups": ["networking.k8s.io"],
                                "apiVersions": ["v1"],
                                "operations": ["CREATE", "UPDATE", "DELETE"],
                                "resources": ["networkpolicies"],
                                "scope": "Namespaced",
                            }
                        ],
                    },
                    {
                        **common,
                        "name": "marker.network.fs2.nebius.ai",
                        "namespaceSelector": {
                            "matchLabels": {"kubernetes.io/metadata.name": "fs2-models"}
                        },
                        "matchConditions": [
                            {
                                "name": "exact-boundary-marker",
                                "expression": "request.name == 'fs2-runtime-network-policy-boundary-v2'",
                            }
                        ],
                        "rules": [
                            {
                                "apiGroups": [""],
                                "apiVersions": ["v1"],
                                "operations": ["CREATE", "UPDATE", "DELETE"],
                                "resources": ["configmaps"],
                                "scope": "Namespaced",
                            },
                        ],
                    },
                    {
                        **common,
                        "name": "helm.network.fs2.nebius.ai",
                        "namespaceSelector": {
                            "matchLabels": {"kubernetes.io/metadata.name": "fs2-system"}
                        },
                        "objectSelector": {
                            "matchLabels": {
                                "name": "fs2-serve-control-plane",
                                "owner": "helm",
                            }
                        },
                        "rules": [
                            {
                                "apiGroups": [""],
                                "apiVersions": ["v1"],
                                "operations": ["CREATE", "UPDATE", "DELETE"],
                                "resources": ["configmaps", "secrets"],
                                "scope": "Namespaced",
                            }
                        ],
                    },
                    {
                        **common,
                        "name": "control-plane.network.fs2.nebius.ai",
                        "namespaceSelector": {
                            "matchLabels": {"kubernetes.io/metadata.name": "fs2-system"}
                        },
                        "objectSelector": {
                            "matchLabels": {
                                "app.kubernetes.io/instance": "fs2-serve-control-plane"
                            }
                        },
                        "rules": [
                            {
                                "apiGroups": [""],
                                "apiVersions": ["v1"],
                                "operations": ["CREATE", "UPDATE", "DELETE"],
                                "resources": [
                                    "configmaps",
                                    "secrets",
                                    "serviceaccounts",
                                    "services",
                                    "persistentvolumeclaims",
                                ],
                                "scope": "Namespaced",
                            },
                            {
                                "apiGroups": ["apps"],
                                "apiVersions": ["v1"],
                                "operations": ["CREATE", "UPDATE", "DELETE"],
                                "resources": [
                                    "deployments",
                                    "statefulsets",
                                    "daemonsets",
                                ],
                                "scope": "Namespaced",
                            },
                            {
                                "apiGroups": ["batch"],
                                "apiVersions": ["v1"],
                                "operations": ["CREATE", "UPDATE", "DELETE"],
                                "resources": ["jobs", "cronjobs"],
                                "scope": "Namespaced",
                            },
                            {
                                "apiGroups": ["autoscaling"],
                                "apiVersions": ["v2"],
                                "operations": ["CREATE", "UPDATE", "DELETE"],
                                "resources": ["horizontalpodautoscalers"],
                                "scope": "Namespaced",
                            },
                            {
                                "apiGroups": ["networking.k8s.io"],
                                "apiVersions": ["v1"],
                                "operations": ["CREATE", "UPDATE", "DELETE"],
                                "resources": ["ingresses", "networkpolicies"],
                                "scope": "Namespaced",
                            },
                            {
                                "apiGroups": ["policy"],
                                "apiVersions": ["v1"],
                                "operations": ["CREATE", "UPDATE", "DELETE"],
                                "resources": ["poddisruptionbudgets"],
                                "scope": "Namespaced",
                            },
                            {
                                "apiGroups": ["rbac.authorization.k8s.io"],
                                "apiVersions": ["v1"],
                                "operations": ["CREATE", "UPDATE", "DELETE"],
                                "resources": ["roles", "rolebindings"],
                                "scope": "Namespaced",
                            },
                        ],
                    },
                    {
                        **common,
                        "name": "release-writers.network.fs2.nebius.ai",
                        "namespaceSelector": {
                            "matchLabels": {"kubernetes.io/metadata.name": "fs2-system"}
                        },
                        "matchConditions": transition._finite_release_match_conditions(
                            RELEASE_INVENTORY
                        ),
                        "rules": [
                            {
                                "apiGroups": ["*"],
                                "apiVersions": ["*"],
                                "operations": ["CREATE", "UPDATE", "DELETE"],
                                "resources": ["*"],
                                "scope": "Namespaced",
                            }
                        ],
                    },
                    {
                        **common,
                        "name": "release-identities.network.fs2.nebius.ai",
                        "namespaceSelector": {
                            "matchLabels": {"kubernetes.io/metadata.name": "fs2-system"}
                        },
                        "matchConditions": [
                            {
                                "name": "exact-release-identity",
                                "expression": (
                                    'request.userInfo.username in '
                                    '["fs2-model-network-authorizer", '
                                    '"fs2-model-network-maintenance", '
                                    '"fs2-model-network-transition"]'
                                ),
                            }
                        ],
                        "rules": [
                            {
                                "apiGroups": ["*"],
                                "apiVersions": ["*"],
                                "operations": ["CREATE", "UPDATE", "DELETE"],
                                "resources": ["*"],
                                "scope": "Namespaced",
                            }
                        ],
                    },
                    {
                        **common,
                        "name": "lease.network.fs2.nebius.ai",
                        "namespaceSelector": {
                            "matchLabels": {"kubernetes.io/metadata.name": "fs2-system"}
                        },
                        "objectSelector": {
                            "matchLabels": {
                                "fs2-serve.nebius.ai/network-boundary-object": "true"
                            }
                        },
                        "matchConditions": [
                            {
                                "name": "exact-boundary-lease",
                                "expression": (
                                    "request.name in ['fs2-model-network-transition', "
                                    "'fs2-model-network-maintenance']"
                                ),
                            }
                        ],
                        "rules": [
                            {
                                "apiGroups": ["coordination.k8s.io"],
                                "apiVersions": ["v1"],
                                "operations": ["CREATE", "UPDATE", "DELETE"],
                                "resources": ["leases"],
                                "scope": "Namespaced",
                            }
                        ],
                    },
                ],
            }
        ]
    }


def boundary_authority() -> dict[str, object]:
    webhook = boundary_webhooks()["items"][0]
    payload: dict[str, object] = {
        "schema": "fs2-serve.nebius.ai/model-network-boundary-authority/v4",
        "phase": "armed",
        "cluster_id": "mk8scluster-test",
        "authority_namespace": "fs2-network-security",
        "webhook": {
            "name": "fs2-model-network-boundary",
            "uid": "uid-boundary-webhook",
            "resource_version": "rv-boundary-webhook",
            "spec_sha256": transition._sha256({"webhooks": webhook["webhooks"]}),
        },
        "tls": {
            "webhook_ca_bundle_sha256": transition._sha256(
                {"caBundle": "dGVzdC1jYQ=="}
            )
        },
        "service": {
            "object_sha256": "1" * 64,
            "endpoints_uid": "uid-boundary-endpoints",
            "endpoints_resource_version": "rv-boundary-endpoints",
            "endpoints_object_sha256": "2" * 64,
            "ready_endpoints": [
                {"node_name": "node-a"},
                {"node_name": "node-b"},
            ],
            "serving_certificate_sha256": "3" * 64,
        },
        "external_custody": {
            "schema": "fs2-serve.nebius.ai/model-network-boundary-provider-custody/v8",
            "policy_id": "network-boundary-test",
            "policy_revision": "1",
            "provider_trust_root_sha256": "f" * 64,
            "signature_verifier_sha256": "e" * 64,
            "client_tools_sha256": "1" * 64,
            "credential_epochs_sha256": "d" * 64,
            "attestation_sha256": "4" * 64,
            "stable_policy_sha256": "8" * 64,
            "gateway_policy_sha256": "7" * 64,
            "provider_inventory_sha256": "9" * 64,
            "kubernetes_authorization_sha256": "a" * 64,
            "provider_authority_census_sha256": "b" * 64,
            "gateway_runtime_measurements_sha256": "c" * 64,
            "gateway_member_ids": ["gateway-a", "gateway-b"],
            "cluster_resource_version": 17,
            "gateway_egress_host_cidrs": [
                "192.0.2.10/32",
                "192.0.2.11/32",
            ],
            "freeze_transaction_id": "reviewer:1:" + "5" * 32,
            "freeze_expires_at": "2026-09-16T18:15:00Z",
            "frozen_resources_sha256": "6" * 64,
        },
        "jobset_writer": {
            "username": (
                "system:serviceaccount:jobset-system:fs2-testrun-jobset-controller"
            )
        },
        "release_inventory": {
            "sha256": transition.hashlib.sha256(
                json.dumps(
                    RELEASE_INVENTORY, sort_keys=True, separators=(",", ":")
                ).encode()
            ).hexdigest(),
            "entries": RELEASE_INVENTORY,
        },
    }
    return {**payload, "payload_sha256": transition._sha256(payload)}


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
        boundary_webhooks(),
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
    assert receipt["schema"].endswith("/v8")
    assert receipt["boundary_endpoints_sha256"] == "2" * 64
    assert receipt["boundary_ready_endpoints_sha256"] == transition._sha256(
        {"ready_endpoints": [{"node_name": "node-a"}, {"node_name": "node-b"}]}
    )
    assert (
        receipt["boundary_authority_sha256"]
        == prepare_contract()["boundary_authority"]["payload_sha256"]
    )
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

    runtime_template = {
        "metadata": {"labels": runtime_labels},
        "spec": secure_pod_spec(),
    }
    keeper_template = {
        "metadata": {"labels": keeper_labels},
        "spec": secure_pod_spec(),
    }
    job_template = {
        "metadata": {"labels": job_labels},
        "spec": secure_pod_spec(),
    }
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
    monkeypatch.setenv("FS2_NETWORK_TRANSITION_LOCK_IDENTITY", LOCK_HOLDER)
    result = transition.verify_enforce(
        contract,
        receipt,
        workload_resources(deployment()),
        {"items": [pod()]},
        controller_deployments(),
        controller_pods(),
        admission_policies(),
        admission_bindings(),
        boundary_webhooks(),
        transition_leases(holder=LOCK_HOLDER),
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
            boundary_webhooks(),
            transition_leases(holder=LOCK_HOLDER),
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
            boundary_webhooks(),
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
            boundary_webhooks(),
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
            boundary_webhooks(),
            transition_leases(holder=LOCK_HOLDER),
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
            boundary_webhooks(),
            leases,
            captured_at="2026-09-16T18:00:00Z",
        )

    webhooks = boundary_webhooks()
    webhooks["items"][0]["webhooks"][0]["matchConditions"] = [
        {"name": "bypass", "expression": "false"}
    ]
    with pytest.raises(transition.ReceiptError, match="matchConditions are not exact"):
        transition.inventory_receipt(
            prepare_contract(),
            workload_resources(deployment()),
            {"items": [pod()]},
            controller_deployments(),
            controller_pods(),
            admission_policies(),
            admission_bindings(),
            webhooks,
            transition_leases(),
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
                boundary_webhooks(),
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
                boundary_webhooks(),
                transition_leases(),
                captured_at="2026-09-16T18:00:00Z",
            )


def test_apply_verifier_binds_admission_resource_versions_and_all_semantics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    contract = prepare_contract()
    contract["phase"] = "enforce"
    receipt = capture(contract=contract)
    monkeypatch.setenv("FS2_NETWORK_TRANSITION_LOCK_IDENTITY", LOCK_HOLDER)

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
            boundary_webhooks(),
            transition_leases(holder=LOCK_HOLDER),
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
            boundary_webhooks(),
            transition_leases(holder=LOCK_HOLDER),
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
    assert (
        "boundary_webhook_name         = local.model_runtime_boundary_webhook_name"
        in source
    )
    assert (
        'model_runtime_transition_writer                       = "fs2-model-network-transition"'
        in source
    )
    assert source.count("request.operation != 'CREATE' ||") >= 6
    assert (
        'model_runtime_acquisition_writer                      = '
        '"system:serviceaccount:fs2-system:fs2-catalog-acquisition"'
        in source
    )
    assert "webhook-verified expiry takeover" in source
    assert (
        "boundary_authority            = var.model_network_boundary_authority_receipt"
        in source
    )
    assert (
        "resource_version"
        in (
            ROOT / "stages/workloads/scripts/model_network_policy_transition.py"
        ).read_text()
    )


def test_external_boundary_authority_is_separate_and_narrowly_scoped() -> None:
    control_plane = (ROOT / "stages/workloads/control_plane.tf").read_text()
    control_plane_schema = json.loads(
        (
            ROOT / "charts/control-plane/fs2-serve-control-plane/values.schema.json"
        ).read_text()
    )
    chart = ROOT / "charts/network-boundary/fs2-model-network-boundary"
    webhook = (chart / "templates/webhook.yaml").read_text()
    authority = (chart / "templates/authority.yaml").read_text()
    rbac = (chart / "templates/rbac.yaml").read_text()
    custody = (chart / "templates/custody-reader-rbac.yaml").read_text()
    static_custody = (chart / "templates/static-custody.yaml").read_text()
    helm_writer = (chart / "templates/helm-writer-rbac.yaml").read_text()
    image_source = (
        ROOT / "components/control-plane/Dockerfile.network-boundary"
    ).read_text()
    image_context = (
        ROOT / "components/control-plane/Dockerfile.network-boundary.dockerignore"
    ).read_text()
    wrapper = (ROOT / "inference-stack").read_text()
    receipt_schema = json.loads(
        (
            ROOT
            / "stages/workloads/contracts/model-network-boundary-authority-receipt.schema.json"
        ).read_text()
    )
    provider_schema = json.loads(
        (
            ROOT
            / "stages/workloads/contracts/model-network-boundary-provider-custody.schema.json"
        ).read_text()
    )

    assert "enabled                   = false" in control_plane
    assert control_plane_schema["properties"]["networkBoundaryAdmission"]["properties"][
        "enabled"
    ] == {"const": False}
    assert "fs2-network-security" in webhook
    assert webhook.count("failurePolicy: Fail") == 8
    assert "control-plane.network.fs2.nebius.ai" in webhook
    assert "helm.network.fs2.nebius.ai" in webhook
    assert "lease.network.fs2.nebius.ai" in webhook
    assert "release-writers.network.fs2.nebius.ai" in webhook
    assert "release-identities.network.fs2.nebius.ai" in webhook
    assert "release-writers-cluster.network.fs2.nebius.ai" not in webhook
    assert "custody.network.fs2.nebius.ai" not in webhook
    assert "cluster-custody.network.fs2.nebius.ai" not in webhook
    assert "admission.network.fs2.nebius.ai" not in webhook
    assert "namespaceSelector:" in webhook
    assert "objectSelector:" in webhook
    model_hook = webhook.split("- name: models.network.fs2.nebius.ai", 1)[1].split(
        "- name: marker.network.fs2.nebius.ai", 1
    )[0]
    marker_hook = webhook.split("- name: marker.network.fs2.nebius.ai", 1)[1].split(
        "- name: helm.network.fs2.nebius.ai", 1
    )[0]
    assert "objectSelector:" not in model_hook
    assert 'resources: ["networkpolicies"]' in model_hook
    assert "request.name == 'fs2-runtime-network-policy-boundary-v2'" in marker_hook
    assert 'resources: ["configmaps"]' in marker_hook
    assert "network-boundary-authority" in authority
    assert 'ENTRYPOINT ["fs2-network-boundary"]' in image_source
    assert "ai.nebius.fs2-network-boundary.source-tree" in image_source
    assert image_context.startswith("**\n")
    assert "**/*secret*" in image_context
    assert "network-boundary-authority" in rbac
    assert "fs2-catalog-acquisition" in rbac
    assert "secrets" not in custody.split("rules:", 1)[1].split("---", 1)[0]
    assert "kind: ValidatingAdmissionPolicy" in static_custody
    assert "fs2-model-network-static-custody" in static_custody
    assert "fs2-model-network-impersonation-guard" in static_custody
    assert "newProtectedBinding" in static_custody
    assert "authorityControlClusterRoles" in static_custody
    assert "authorityControlRoles" in static_custody
    assert "request.operation != 'DELETE'" in static_custody
    assert "certificateController" in static_custody
    assert "request.operation in ['CREATE', 'UPDATE']" in static_custody
    assert "caBundleOnly" not in static_custody
    assert "variables.oldMetadata" in static_custody
    assert "scientificWriterServiceAccountName" in static_custody
    assert "jobsetWriterUsername" in static_custody
    assert "fs2-model-network-helm-writer" in helm_writer
    assert "fs2-model-network-maintenance" in helm_writer
    assert receipt_schema["properties"]["schema"]["const"].endswith("/v4")
    assert provider_schema["properties"]["schema"]["const"].endswith("/v8")
    assert "protected_kubernetes_resources" in provider_schema["properties"][
        "mutation_freeze"
    ]["required"]
    assert provider_schema["properties"]["mutation_freeze"]["properties"][
        "allowed_principal_ids"
    ] == {"const": []}
    assert {"serving_secret", "ca_secret", "webhook_ca_bundle_sha256"} == set(
        receipt_schema["properties"]["tls"]["required"]
    )
    assert {
        "endpoints_uid",
        "endpoints_resource_version",
        "endpoints_object_sha256",
    }.issubset(receipt_schema["properties"]["service"]["required"])
    assert "expires_at - issued_at > timedelta(minutes=15)" in wrapper
    assert "observed_keys != required" in wrapper
    assert "live identity-mint bindings differ" in wrapper
    assert (
        "mutation freeze does not cover every receipt-bound authority and writer object"
        in wrapper
    )
    assert "revalidated_receipt != authority_receipt" in wrapper


def test_provider_custody_closes_in_cluster_ha_inventory_and_apply_expiry_paths() -> None:
    wrapper = (ROOT / "inference-stack").read_text()
    provider = (ROOT / "stages/workloads/provider_custody.tf").read_text()
    transition = (ROOT / "stages/workloads/network_policies.tf").read_text()
    gateway = (
        ROOT / "components/control-plane/src/fs2_serve/provider_custody_gateway.py"
    ).read_text()
    entrypoint = (
        ROOT / "components/control-plane/src/fs2_serve/provider_custody_entrypoint.py"
    ).read_text()
    static_custody = (
        ROOT
        / "charts/network-boundary/fs2-model-network-boundary/templates/static-custody.yaml"
    ).read_text()
    schema = json.loads(
        (
            ROOT
            / "stages/workloads/contracts/model-network-boundary-provider-custody.schema.json"
        ).read_text()
    )

    assert '["iam", "project", "get", "--id", project_id]' in wrapper
    assert '["mk8s", "cluster", "get", "--id", cluster_id]' in wrapper
    assert '["compute", "instance", "list", "--parent-id", project_id]' in wrapper
    assert '["vpc", "security-group", "list", "--parent-id", project_id]' in wrapper
    assert '["iam", "service-account", "list", "--parent-id", project_id]' in wrapper
    assert "owning an exact live API-allowlist address" in wrapper
    assert "FS2_NETWORK_BOUNDARY_PROVIDER_AUTHORITY_EXPORTER" in wrapper
    assert "provider-control-plane-authority-observation/v1" in wrapper
    assert "for _index in range(2)" in wrapper
    assert "provider-control-plane-deny-all-exact-resources-and-scope-collections" in wrapper
    assert "provider-native-immutable-until-active-until" in wrapper
    assert "authority_api_server_certificate_sha256" in wrapper
    assert "front_proxy_enabled" in wrapper
    assert "measurement_resource_id" in wrapper
    assert "all_mutation_authorities_included" in wrapper
    assert "all_gateway_network_paths_included" in wrapper
    assert "all_apply_journal_records_included" in wrapper
    assert "provider.apply-journal" in wrapper
    assert "receipt_signing_public_key_sha256" in wrapper
    assert "server_certificate_sha256" in wrapper
    assert "socket.create_connection" in wrapper
    assert '"jobset.x-k8s.io": {"jobsets"}' in wrapper
    assert '"batch": {"cronjobs", "jobs"}' in wrapper
    authority_predicate = wrapper.split(
        "def grants_authority_control_access", 1
    )[1].split("def binding_subjects", 1)[0]
    assert '"validatingadmissionpolicies"' in authority_predicate
    assert '"validatingwebhookconfigurations"' in authority_predicate
    assert '"pods"' not in authority_predicate
    assert '"deployments"' not in authority_predicate
    assert '"jobs"' not in authority_predicate
    assert "direct mutation authority over an admission object" in wrapper
    assert "scientificWriterServiceAccountName" in static_custody
    assert "request.namespace == 'jobset-system'" in static_custody
    assert "ssl.CERT_REQUIRED" in entrypoint
    assert "ssl.TLSVersion.TLSv1_3" in entrypoint
    assert 'get_extra_info("ssl_object")' in entrypoint
    assert "x_fs2_provider_client_certificate" not in gateway
    assert "process.wait(timeout=5)" in wrapper
    assert "provider custody expiry approached during apply" in wrapper
    assert "post-apply fence" in wrapper
    assert "server_certificate_sha256" in provider
    assert 'metadata.labels["security-boundary"]' not in transition
    assert schema["properties"]["provider_enumeration"]["properties"][
        "service_accounts_sha256"
    ]["$ref"] == "#/$defs/sha256"
    assert schema["properties"]["schema"]["const"].endswith("/v8")
    assert "provider_authority_census" in schema["required"]
    assert "apply_journal" in schema["$defs"]["providerAuthorityCensus"][
        "properties"
    ]["snapshot"]["required"]
    apply_journal = schema["$defs"]["providerAuthorityCensus"]["properties"][
        "snapshot"
    ]["properties"]["apply_journal"]
    assert apply_journal["properties"]["journal_protocol"]["const"].endswith(
        "/v2"
    )
    assert apply_journal["properties"]["page_size"]["const"] == 256
    assert apply_journal["properties"]["history_retention"]["const"] == (
        "append-only-unbounded"
    )
    assert schema["$defs"]["providerAuthorityCensus"]["properties"][
        "snapshot"
    ]["properties"]["provider_mutation_freeze"]["properties"][
        "enforcement"
    ]["const"] == "provider-control-plane-deny-all-exact-resources-and-scope-collections"
    assert "server_certificate_sha256" in schema["$defs"]["gatewayMember"][
        "required"
    ]


def test_provider_apply_journal_is_paginated_and_checkpoint_complete() -> None:
    wrapper = (ROOT / "inference-stack").read_text()
    journal = json.loads(
        (
            ROOT
            / "stages/workloads/contracts/model-network-provider-apply-journal.schema.json"
        ).read_text()
    )
    request = json.loads(
        (
            ROOT
            / "stages/workloads/contracts/model-network-provider-apply-journal-request.schema.json"
        ).read_text()
    )

    assert journal["properties"]["schema"]["const"].endswith("/v2")
    assert journal["properties"]["records"]["maxItems"] == 256
    assert "checkpoint_signature" in journal["required"]
    assert journal["$defs"]["checkpoint"]["properties"][
        "deletion_supported"
    ]["const"] is False
    assert journal["$defs"]["checkpoint"]["properties"][
        "history_accumulator_algorithm"
    ]["const"] == "sha256-append-event-chain-v1"
    assert journal["$defs"]["checkpoint"]["properties"][
        "unresolved_accumulator_algorithm"
    ]["const"] == "sha256-sorted-signed-record-chain-v1"
    observe = request["$defs"]["observe"]["allOf"][1]
    assert set(observe["required"]) == {
        "view",
        "cursor",
        "expected_checkpoint_sha256",
        "page_size",
    }
    assert observe["properties"]["page_size"]["const"] == 256
    assert "expected_checkpoint_sha256" in request["$defs"]["begin"][
        "allOf"
    ][1]["required"]
    assert "expected_checkpoint_sha256" in request["$defs"]["resolve"][
        "allOf"
    ][1]["required"]
    assert "_complete_unresolved_provider_apply_journal(" in wrapper
    assert "provider apply-journal range proof has a gap or overlap" in wrapper
    assert "len(records) > 4096" not in wrapper


def test_terraform_enforcement_orders_apply_fence_before_default_deny() -> None:
    source = (ROOT / "stages/workloads/network_policies.tf").read_text()
    lease_guard = source.split("model_runtime_lease_guard_admission_policy_spec = {", 1)[1].split(
        "model_runtime_transition_guard_resource_rules = [", 1
    )[0]
    default_deny = source.split(
        'resource "kubernetes_network_policy_v1" "model_namespace_default_deny"', 1
    )[1]
    assert "terraform_data.model_runtime_network_policy_apply_fence" in default_deny
    assert 'provisioner "local-exec"' in source
    assert "verify-enforce" in source
    assert "model_runtime_transition_writer_identity_expression" in lease_guard
    assert "model_runtime_active_transition_writer_expression" not in lease_guard
    assert "params." not in lease_guard
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
