from __future__ import annotations

import copy
import hashlib
import importlib.util
from pathlib import Path
from types import ModuleType


MODULE_PATH = (
    Path(__file__).parents[1]
    / "security/customer-storage-egress-boundary/protected_lane_admission.py"
)


def load_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("protected_lane_admission", MODULE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ADMISSION = load_module()


def lane_for(generation: str) -> str:
    return f"l{generation[1:15]}-{hashlib.sha256(generation.encode()).hexdigest()[:12]}"


LANE_ROLES = ("otel-node", "gpu-allocation-observer")
NODE_AGENT_ROLES = (
    "filesystem-csi",
    "prometheus-node-exporter",
    "retained-otel-node",
)


def observer_spec(generation: str, role: str) -> dict[str, object]:
    lane_id = lane_for(generation)
    scheduling_key = f"workload.fs2.nebius/customer-storage-egress-{lane_id[-12:]}"
    labels: dict[str, str] = {
        "app.kubernetes.io/name": f"fs2-{role}",
        "app.kubernetes.io/component": role,
    }
    lane_scoped = role in LANE_ROLES
    if lane_scoped:
        labels["fs2.nebius.ai/protected-lane-id"] = lane_id
    scheduling = (
        {
            "nodeSelector": {scheduling_key: lane_id},
            "tolerations": [
                {
                    "key": scheduling_key,
                    "operator": "Equal",
                    "value": lane_id,
                    "effect": "NoSchedule",
                }
            ],
        }
        if lane_scoped
        else {"tolerations": [{"operator": "Exists"}]}
    )
    return {
        "selector": {"matchLabels": labels},
        "template": {
            "metadata": {"labels": labels},
            "spec": {
                "serviceAccountName": f"fs2-{role}-{lane_id[-12:]}",
                "automountServiceAccountToken": False,
                **scheduling,
                "containers": [{"name": role, "image": "example.invalid/image@sha256:" + "a" * 64}],
            },
        },
    }


def contract(generation: str) -> dict[str, object]:
    lane_id = lane_for(generation)
    suffix = lane_id[-12:]
    observers: dict[str, object] = {}
    for index, role in enumerate(LANE_ROLES + NODE_AGENT_ROLES, start=1):
        spec = observer_spec(generation, role)
        observers[role] = {
            "namespace": "kube-system",
            "name": (
                f"fs2-{role}-{suffix}"
                if role in LANE_ROLES
                else f"fs2-retained-{role}"
            ),
            "uid": f"00000000-0000-4000-8000-{index:012d}",
            "owner_username": f"fs2:{role}-release:{lane_id}",
            "daemonset_spec": spec,
            "daemonset_spec_sha256": ADMISSION.digest(spec),
        }
    scheduling_key = f"workload.fs2.nebius/customer-storage-egress-{suffix}"
    protected_node_names = ["computeinstance-protected"]
    result = {
        "schema": "fs2-serve.nebius.ai/protected-lane-admission/v2",
        "generation": generation,
        "lane_id": lane_id,
        "selector_key": scheduling_key,
        "selector_value": lane_id,
        "taint_key": scheduling_key,
        "taint_value": lane_id,
        "taint_effect": "NoSchedule",
        "protected_node_names": protected_node_names,
        "protected_node_inventory_sha256": ADMISSION.digest(protected_node_names),
        "daemonset_controller_username": "system:controller:daemon-set-controller",
        "scheduler_username": "system:kube-scheduler",
        "observers": observers,
        "observer_inventory_sha256": ADMISSION.digest(observers),
    }
    ADMISSION.validate_contract(result)
    return result


def observer_pod_request(value: dict[str, object], role: str) -> dict[str, object]:
    observer = value["observers"][role]
    template = observer["daemonset_spec"]["template"]
    labels = copy.deepcopy(template["metadata"]["labels"])
    labels.update({"controller-revision-hash": "abc123", "pod-template-generation": "1"})
    pod_spec = copy.deepcopy(template["spec"])
    if role in NODE_AGENT_ROLES:
        pod_spec["affinity"] = {
            "nodeAffinity": {
                "requiredDuringSchedulingIgnoredDuringExecution": {
                    "nodeSelectorTerms": [
                        {
                            "matchFields": [
                                {
                                    "key": "metadata.name",
                                    "operator": "In",
                                    "values": value["protected_node_names"],
                                }
                            ]
                        }
                    ]
                }
            }
        }
    return {
        "resource": "pods",
        "operation": "CREATE",
        "namespace": observer["namespace"],
        "username": value["daemonset_controller_username"],
        "object": {
            "metadata": {
                "name": observer["name"] + "-protected",
                "labels": labels,
                "ownerReferences": [
                    {
                        "apiVersion": "apps/v1",
                        "kind": "DaemonSet",
                        "name": observer["name"],
                        "uid": observer["uid"],
                        "controller": True,
                        "blockOwnerDeletion": True,
                    }
                ],
            },
            "spec": pod_spec,
        },
    }


def test_retained_and_successor_conjunction_allows_exact_lane_successors() -> None:
    predecessor = contract("g20260917010000-111111111111")
    successor = contract("g20260917020000-222222222222")

    for role in LANE_ROLES:
        request = observer_pod_request(successor, role)
        assert not ADMISSION.request_targets_lane(request, predecessor)
        assert ADMISSION.successor_allows(request, successor)
        assert ADMISSION.conjunction_allows(
            request, retained=[predecessor], successor=successor
        )


def test_retained_broad_kube_system_exception_cannot_admit_rogue_blanket_pod() -> None:
    predecessor = contract("g20260917010000-111111111111")
    successor = contract("g20260917020000-222222222222")
    request = {
        "resource": "pods",
        "operation": "CREATE",
        "namespace": "kube-system",
        "username": "system:controller:daemon-set-controller",
        "object": {
            "metadata": {
                "name": "rogue-node-a",
                "labels": {"app": "rogue"},
                "ownerReferences": [
                    {
                        "apiVersion": "apps/v1",
                        "kind": "DaemonSet",
                        "name": "rogue",
                        "uid": "00000000-0000-4000-8000-999999999999",
                        "controller": True,
                        "blockOwnerDeletion": True,
                    }
                ],
            },
            "spec": {
                "affinity": {
                    "nodeAffinity": {
                        "requiredDuringSchedulingIgnoredDuringExecution": {
                            "nodeSelectorTerms": [
                                {
                                    "matchFields": [
                                        {
                                            "key": "metadata.name",
                                            "operator": "In",
                                            "values": successor["protected_node_names"],
                                        }
                                    ]
                                }
                            ]
                        }
                    }
                },
                "tolerations": [{"operator": "Exists"}],
            },
        },
    }

    assert ADMISSION.predecessor_allows(request, predecessor)
    assert not ADMISSION.successor_allows(request, successor)
    assert not ADMISSION.conjunction_allows(
        request, retained=[predecessor], successor=successor
    )


def test_exact_observer_owner_cannot_forge_child_service_account_or_image() -> None:
    successor = contract("g20260917020000-222222222222")
    request = observer_pod_request(successor, "otel-node")
    assert ADMISSION.successor_allows(request, successor)

    request["object"]["spec"]["serviceAccountName"] = "default"
    request["object"]["spec"]["containers"][0]["image"] = (
        "example.invalid/other@sha256:" + "b" * 64
    )
    assert not ADMISSION.successor_allows(request, successor)


def test_fresh_install_denies_lane_targeting_daemonset_but_allows_exact_update() -> None:
    successor = contract("g20260917020000-222222222222")
    rogue = {
        "resource": "daemonsets",
        "operation": "CREATE",
        "namespace": "kube-system",
        "username": "fs2:rogue-release",
        "object": {
            "metadata": {"name": "rogue"},
            "spec": {
                "template": {
                    "spec": {
                        "affinity": {
                            "nodeAffinity": {
                                "requiredDuringSchedulingIgnoredDuringExecution": {
                                    "nodeSelectorTerms": [
                                        {
                                            "matchExpressions": [
                                                {
                                                    "key": successor["selector_key"],
                                                    "operator": "Exists",
                                                }
                                            ]
                                        }
                                    ]
                                }
                            }
                        },
                        "tolerations": [
                            {
                                "key": successor["taint_key"],
                                "operator": "Exists",
                            }
                        ],
                    }
                }
            },
        },
    }
    assert ADMISSION.request_targets_lane(rogue, successor)
    assert not ADMISSION.successor_allows(rogue, successor)

    observer = successor["observers"]["otel-node"]
    exact_object = {
        "metadata": {"name": observer["name"], "uid": observer["uid"]},
        "spec": copy.deepcopy(observer["daemonset_spec"]),
    }
    exact = {
        "resource": "daemonsets",
        "operation": "UPDATE",
        "namespace": observer["namespace"],
        "username": observer["owner_username"],
        "object": copy.deepcopy(exact_object),
        "old_object": copy.deepcopy(exact_object),
    }
    assert ADMISSION.successor_allows(exact, successor)


def test_update_matches_old_or_new_lane_path_using_lane_id_not_generation() -> None:
    successor = contract("g20260917020000-222222222222")
    key = successor["selector_key"]
    old = {
        "metadata": {"name": "rogue"},
        "spec": {
            "template": {
                "spec": {
                    "nodeSelector": {key: successor["selector_value"]},
                    "tolerations": [
                        {
                            "key": successor["taint_key"],
                            "operator": "Equal",
                            "value": successor["taint_value"],
                            "effect": successor["taint_effect"],
                        }
                    ],
                }
            }
        },
    }
    for new_spec in (
        {"nodeSelector": {"workload.fs2.nebius/unrelated": "true"}},
        {
            "affinity": {
                "nodeAffinity": {
                    "requiredDuringSchedulingIgnoredDuringExecution": {
                        "nodeSelectorTerms": [
                            {
                                "matchExpressions": [
                                    {
                                        "key": key,
                                        "operator": "In",
                                        "values": [successor["selector_value"]],
                                    }
                                ]
                            }
                        ]
                    }
                }
            }
        },
    ):
        request = {
            "resource": "daemonsets",
            "operation": "UPDATE",
            "namespace": "kube-system",
            "username": "fs2:rogue-release",
            "old_object": copy.deepcopy(old),
            "object": {
                "metadata": {"name": "rogue"},
                "spec": {"template": {"spec": new_spec}},
            },
        }
        assert ADMISSION.request_targets_lane(request, successor)
        assert not ADMISSION.successor_allows(request, successor)


def test_generation_unique_key_prevents_retained_policy_cross_denial() -> None:
    predecessor = contract("g20260917010000-111111111111")
    successor = contract("g20260917020000-222222222222")
    request = {
        "resource": "pods",
        "operation": "CREATE",
        "namespace": "fs2-system",
        "username": "system:controller:replicaset-controller",
        "storage_contract": True,
        "object": {
            "metadata": {"name": "storage-successor"},
            "spec": {
                "nodeSelector": {
                    successor["selector_key"]: successor["selector_value"]
                },
                "tolerations": [
                    {
                        "key": successor["taint_key"],
                        "operator": "Equal",
                        "value": successor["taint_value"],
                        "effect": successor["taint_effect"],
                    }
                ],
            },
        },
    }
    assert not ADMISSION.request_targets_lane(request, predecessor)
    assert ADMISSION.conjunction_allows(
        request, retained=[predecessor], successor=successor
    )


def test_keyed_exists_plus_required_lane_affinity_is_matched() -> None:
    successor = contract("g20260917020000-222222222222")
    request = {
        "resource": "pods",
        "operation": "CREATE",
        "namespace": "fs2-system",
        "object": {
            "metadata": {"name": "keyed-exists"},
            "spec": {
                "affinity": {
                    "nodeAffinity": {
                        "requiredDuringSchedulingIgnoredDuringExecution": {
                            "nodeSelectorTerms": [
                                {
                                    "matchExpressions": [
                                        {
                                            "key": successor["selector_key"],
                                            "operator": "In",
                                            "values": [successor["selector_value"]],
                                        }
                                    ]
                                }
                            ]
                        }
                    }
                },
                "tolerations": [
                    {
                        "key": successor["taint_key"],
                        "operator": "Exists",
                        "effect": "NoSchedule",
                    }
                ],
            },
        },
    }
    assert ADMISSION.request_targets_lane(request, successor)


def test_equal_toleration_with_omitted_effect_is_matched() -> None:
    successor = contract("g20260917020000-222222222222")
    request = {
        "resource": "deployments",
        "operation": "CREATE",
        "namespace": "fs2-system",
        "object": {
            "metadata": {"name": "omitted-effect"},
            "spec": {
                "template": {
                    "spec": {
                        "tolerations": [
                            {
                                "key": successor["taint_key"],
                                "operator": "Equal",
                                "value": successor["taint_value"],
                            }
                        ]
                    }
                }
            },
        },
    }
    assert ADMISSION.request_targets_lane(request, successor)


def test_direct_node_name_is_matched_without_a_toleration() -> None:
    successor = contract("g20260917020000-222222222222")
    request = {
        "resource": "pods",
        "operation": "CREATE",
        "namespace": "fs2-system",
        "object": {
            "metadata": {"name": "direct"},
            "spec": {"nodeName": "computeinstance-protected"},
        },
    }
    assert ADMISSION.request_targets_lane(request, successor)
    assert not ADMISSION.successor_allows(request, successor)

    request["object"]["spec"]["nodeName"] = "computeinstance-unrelated"
    assert not ADMISSION.request_targets_lane(request, successor)


def test_affinity_without_a_matching_toleration_is_not_a_scheduling_path() -> None:
    successor = contract("g20260917020000-222222222222")
    request = {
        "resource": "pods",
        "operation": "CREATE",
        "namespace": "fs2-system",
        "object": {
            "metadata": {"name": "affinity-only"},
            "spec": {
                "affinity": {
                    "nodeAffinity": {
                        "requiredDuringSchedulingIgnoredDuringExecution": {
                            "nodeSelectorTerms": [
                                {
                                    "matchExpressions": [
                                        {
                                            "key": successor["selector_key"],
                                            "operator": "In",
                                            "values": [successor["selector_value"]],
                                        }
                                    ]
                                }
                            ]
                        }
                    }
                }
            },
        },
    }
    assert not ADMISSION.request_targets_lane(request, successor)


def test_blanket_toleration_with_unrelated_selector_is_not_lane_targeting() -> None:
    successor = contract("g20260917020000-222222222222")
    request = {
        "resource": "daemonsets",
        "operation": "UPDATE",
        "namespace": "kube-system",
        "object": {
            "metadata": {"name": "unrelated-node-agent"},
            "spec": {
                "template": {
                    "spec": {
                        "nodeSelector": {"workload.fs2.nebius/system": "true"},
                        "tolerations": [{"operator": "Exists"}],
                    }
                }
            },
        },
        "old_object": {
            "metadata": {"name": "unrelated-node-agent"},
            "spec": {
                "template": {
                    "spec": {
                        "nodeSelector": {"workload.fs2.nebius/system": "true"},
                        "tolerations": [{"operator": "Exists"}],
                    }
                }
            },
        },
    }
    assert not ADMISSION.request_targets_lane(request, successor)


def test_update_matches_both_entry_to_and_exit_from_the_lane() -> None:
    successor = contract("g20260917020000-222222222222")
    lane_spec = {
        "nodeSelector": {successor["selector_key"]: successor["selector_value"]},
        "tolerations": [
            {
                "key": successor["taint_key"],
                "operator": "Equal",
                "value": successor["taint_value"],
                "effect": successor["taint_effect"],
            }
        ],
    }
    ordinary_spec = {"nodeSelector": {"workload.fs2.nebius/unrelated": "true"}}
    for old_spec, new_spec in ((ordinary_spec, lane_spec), (lane_spec, ordinary_spec)):
        request = {
            "resource": "deployments",
            "operation": "UPDATE",
            "namespace": "fs2-system",
            "old_object": {"metadata": {"name": "move"}, "spec": {"template": {"spec": old_spec}}},
            "object": {"metadata": {"name": "move"}, "spec": {"template": {"spec": new_spec}}},
        }
        assert ADMISSION.request_targets_lane(request, successor)

    direct_spec = {"nodeName": "computeinstance-protected"}
    for old_spec, new_spec in ((ordinary_spec, direct_spec), (direct_spec, ordinary_spec)):
        request = {
            "resource": "pods",
            "operation": "UPDATE",
            "namespace": "fs2-system",
            "old_object": {"metadata": {"name": "direct-move"}, "spec": old_spec},
            "object": {"metadata": {"name": "direct-move"}, "spec": new_spec},
        }
        assert ADMISSION.request_targets_lane(request, successor)


def test_existing_critical_node_agents_survive_retained_policy_conjunction() -> None:
    predecessor = contract("g20260917010000-111111111111")
    successor = contract("g20260917020000-222222222222")
    for role in NODE_AGENT_ROLES:
        request = observer_pod_request(successor, role)
        assert ADMISSION.predecessor_allows(request, predecessor)
        assert ADMISSION.successor_allows(request, successor)
        assert ADMISSION.conjunction_allows(
            request, retained=[predecessor], successor=successor
        )
