from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime
from typing import Any

import pytest

from fs2_serve.network_boundary_admission import (
    NetworkBoundaryAdmission,
    NetworkBoundaryConfig,
    NetworkBoundaryError,
    ReleaseInventoryEntry,
)

PROFILE = "job-public-acquisition-v1"
CLASS = "public-acquisition"
MANAGER = "system:kube-controller-manager"
SCIENTIFIC_WRITER = "system:serviceaccount:fs2-system:fs2-scientific-job-writer"


class FakeReader:
    def __init__(self, objects: dict[str, dict[str, Any]] | None = None) -> None:
        self.objects = objects or {}

    async def get(self, path: str) -> dict[str, Any]:
        if path not in self.objects:
            raise NetworkBoundaryError("the referenced live parent does not exist")
        return deepcopy(self.objects[path])

    async def get_optional(self, path: str) -> dict[str, Any] | None:
        value = self.objects.get(path)
        return deepcopy(value) if value is not None else None


def admission(
    reader: FakeReader,
    *,
    now: datetime | None = None,
    release_inventory: tuple[ReleaseInventoryEntry, ...] = (),
) -> NetworkBoundaryAdmission:
    return NetworkBoundaryAdmission(
        config=NetworkBoundaryConfig(
            model_namespace="fs2-models",
            system_namespace="fs2-system",
            authority_namespace="fs2-network-security",
            acquisition_writer=("system:serviceaccount:fs2-system:fs2-catalog-acquisition"),
            direct_job_writer=SCIENTIFIC_WRITER,
            jobset_writer="system:serviceaccount:jobset-system:jobset-controller",
            model_controller_writer=(
                "system:serviceaccount:fs2-system:fs2-serve-control-plane-controller"
            ),
            authorizer_writer="fs2-model-network-authorizer",
            transition_writer="fs2-model-network-transition",
            maintenance_writer="fs2-model-network-maintenance",
            certificate_writer="system:serviceaccount:cert-manager:cert-manager",
            authorizer_groups=frozenset({"fs2:model-network-authorizer", "system:authenticated"}),
            transition_groups=frozenset({"fs2:model-network-transition", "system:authenticated"}),
            maintenance_groups=frozenset({"fs2:model-network-maintenance", "system:authenticated"}),
            certificate_groups=frozenset(
                {
                    "system:authenticated",
                    "system:serviceaccounts",
                    "system:serviceaccounts:cert-manager",
                }
            ),
            release_inventory=release_inventory,
        ),
        reader=reader,  # type: ignore[arg-type]
        clock=(lambda: now) if now is not None else None,
    )


def labels(*, workload_class: str = CLASS, profile: str = PROFILE) -> dict[str, str]:
    return {
        "app.kubernetes.io/part-of": "fs2-serve",
        "fs2-serve.nebius.ai/network-workload-class": workload_class,
        "fs2-serve.nebius.ai/network-profile": profile,
    }


def secure_pod_spec() -> dict[str, Any]:
    return {
        "automountServiceAccountToken": False,
        "securityContext": {
            "runAsNonRoot": True,
            "seccompProfile": {"type": "RuntimeDefault"},
        },
        "containers": [
            {
                "name": "runtime",
                "securityContext": {
                    "allowPrivilegeEscalation": False,
                    "capabilities": {"drop": ["ALL"]},
                },
            }
        ],
    }


def pod_template(workload_labels: dict[str, str]) -> dict[str, Any]:
    return {
        "metadata": {"labels": workload_labels},
        "spec": secure_pod_spec(),
    }


def jobset_spec(workload_labels: dict[str, str]) -> dict[str, Any]:
    return {
        "replicatedJobs": [
            {
                "name": "worker",
                "template": {
                    "metadata": {"labels": workload_labels},
                    "spec": {"template": pod_template(workload_labels)},
                },
            }
        ]
    }


def review(
    *,
    kind: str,
    resource: str,
    value: dict[str, Any],
    username: str,
    operation: str = "CREATE",
    old_value: dict[str, Any] | None = None,
    group: str | None = None,
    namespace: str = "fs2-models",
    groups: list[str] | None = None,
) -> dict[str, Any]:
    if groups is None:
        if username.startswith("system:serviceaccount:"):
            service_account_namespace = username.split(":", 3)[2]
            groups = [
                "system:authenticated",
                "system:serviceaccounts",
                f"system:serviceaccounts:{service_account_namespace}",
            ]
        elif username == MANAGER:
            groups = ["system:authenticated"]
        elif username in {
            "fs2-model-network-authorizer",
            "fs2-model-network-transition",
            "fs2-model-network-maintenance",
        }:
            groups = [username.replace("fs2-", "fs2:", 1), "system:authenticated"]
        else:
            groups = [f"fs2:{username}", "system:authenticated"]
    return {
        "request": {
            "uid": "request-uid",
            "operation": operation,
            "resource": {
                "group": group if group is not None else ("batch" if kind == "Job" else ""),
                "resource": resource,
            },
            "kind": {"kind": kind},
            "namespace": namespace,
            "userInfo": {"username": username, "groups": groups},
            "object": value,
            "oldObject": old_value,
        }
    }


@pytest.mark.asyncio
async def test_direct_public_acquisition_job_requires_normal_catalog_writer() -> None:
    job = {
        "metadata": {"name": "acquire", "labels": labels()},
        "spec": {"template": pod_template(labels())},
    }
    writer = "system:serviceaccount:fs2-system:fs2-catalog-acquisition"
    result = await admission(FakeReader()).review(review(kind="Job", resource="jobs", value=job, username=writer))
    assert result["response"]["allowed"] is True

    for rejected_writer in (
        "fs2-model-network-transition",
        "system:serviceaccount:fs2-system:fs2-serve-control-plane-runtime",
    ):
        with pytest.raises(NetworkBoundaryError, match="exact platform writer"):
            await admission(FakeReader()).review(
                review(
                    kind="Job",
                    resource="jobs",
                    value=job,
                    username=rejected_writer,
                )
            )


@pytest.mark.asyncio
async def test_direct_scientific_job_requires_distinct_writer_and_exact_groups() -> None:
    internal = labels(workload_class="internal-job", profile="job-internal-v1")
    job = {
        "metadata": {"name": "scientific", "labels": internal},
        "spec": {"template": pod_template(internal)},
    }
    result = await admission(FakeReader()).review(
        review(kind="Job", resource="jobs", value=job, username=SCIENTIFIC_WRITER)
    )
    assert result["response"]["allowed"] is True

    with pytest.raises(NetworkBoundaryError, match="group set"):
        await admission(FakeReader()).review(
            review(
                kind="Job",
                resource="jobs",
                value=job,
                username=SCIENTIFIC_WRITER,
                groups=["system:authenticated"],
            )
        )


@pytest.mark.asyncio
async def test_jobset_and_runtime_parent_require_separate_exact_writers() -> None:
    internal = labels(workload_class="internal-job", profile="job-internal-v1")
    jobset = {
        "metadata": {"name": "scientific", "labels": internal},
        "spec": jobset_spec(internal),
    }
    result = await admission(FakeReader()).review(
        review(
            kind="JobSet",
            resource="jobsets",
            value=jobset,
            username=SCIENTIFIC_WRITER,
            group="jobset.x-k8s.io",
        )
    )
    assert result["response"]["allowed"] is True

    runtime = labels(workload_class="runtime", profile="gateway-dns-tcp-8000-v1")
    deployment = {
        "metadata": {"name": "runtime", "labels": runtime},
        "spec": {"template": pod_template(runtime)},
    }
    controller = "system:serviceaccount:fs2-system:fs2-serve-control-plane-controller"
    result = await admission(FakeReader()).review(
        review(
            kind="Deployment",
            resource="deployments",
            value=deployment,
            username=controller,
            group="apps",
        )
    )
    assert result["response"]["allowed"] is True

    with pytest.raises(NetworkBoundaryError, match="model-controller writer"):
        await admission(FakeReader()).review(
            review(
                kind="Deployment",
                resource="deployments",
                value=deployment,
                username=SCIENTIFIC_WRITER,
                group="apps",
            )
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (("hostNetwork", True), "host namespaces"),
        (("hostPID", True), "host namespaces"),
        (("hostIPC", True), "host namespaces"),
        (("privileged", True), "security envelope"),
        (("capabilities", {"drop": ["ALL"], "add": ["SYS_ADMIN"]}), "security envelope"),
        (("capabilities", {"drop": ["ALL"], "add": ["IPC_LOCK"]}), "security envelope"),
        (("hostPath", {"path": "/"}), "hostPath"),
    ],
)
async def test_profiled_runtime_rejects_host_and_capability_escape(
    mutation: tuple[str, object], message: str
) -> None:
    runtime = labels(workload_class="runtime", profile="gateway-dns-tcp-8000-v1")
    spec = secure_pod_spec()
    field, value = mutation
    if field in {"privileged", "capabilities"}:
        spec["containers"][0]["securityContext"][field] = value
    elif field == "hostPath":
        spec["volumes"] = [{"name": "host", "hostPath": value}]
    else:
        spec[field] = value
    deployment = {
        "metadata": {"name": "runtime", "labels": runtime},
        "spec": {
            "template": {"metadata": {"labels": runtime}, "spec": spec}
        },
    }
    with pytest.raises(NetworkBoundaryError, match=message):
        await admission(FakeReader()).review(
            review(
                kind="Deployment",
                resource="deployments",
                value=deployment,
                username=(
                    "system:serviceaccount:fs2-system:"
                    "fs2-serve-control-plane-controller"
                ),
                group="apps",
            )
        )


@pytest.mark.asyncio
async def test_only_modelexpress_profile_may_add_ipc_lock() -> None:
    runtime = labels(workload_class="runtime", profile="mx-" + "a" * 60)
    spec = secure_pod_spec()
    spec["containers"][0]["securityContext"]["capabilities"]["add"] = [
        "IPC_LOCK"
    ]
    deployment = {
        "metadata": {"name": "modelexpress", "labels": runtime},
        "spec": {"template": {"metadata": {"labels": runtime}, "spec": spec}},
    }
    result = await admission(FakeReader()).review(
        review(
            kind="Deployment",
            resource="deployments",
            value=deployment,
            username=(
                "system:serviceaccount:fs2-system:"
                "fs2-serve-control-plane-controller"
            ),
            group="apps",
        )
    )
    assert result["response"]["allowed"] is True


@pytest.mark.asyncio
async def test_profiled_runtime_allows_only_read_only_published_reference_plane() -> None:
    runtime = labels(workload_class="runtime", profile="gateway-dns-tcp-8000-v1")
    spec = secure_pod_spec()
    spec["volumes"] = [
        {
            "name": "reference-data",
            "hostPath": {
                "path": "/mnt/fs2-reference-data/data",
                "type": "Directory",
            },
        }
    ]
    spec["containers"][0]["volumeMounts"] = [
        {
            "name": "reference-data",
            "mountPath": "/reference-data",
            "readOnly": True,
        }
    ]
    deployment = {
        "metadata": {"name": "runtime", "labels": runtime},
        "spec": {"template": {"metadata": {"labels": runtime}, "spec": spec}},
    }
    result = await admission(FakeReader()).review(
        review(
            kind="Deployment",
            resource="deployments",
            value=deployment,
            username=(
                "system:serviceaccount:fs2-system:"
                "fs2-serve-control-plane-controller"
            ),
            group="apps",
        )
    )
    assert result["response"]["allowed"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["apiVersion", "name", "uid"])
async def test_replicaset_child_is_bound_to_exact_live_deployment(field: str) -> None:
    runtime_labels = labels(workload_class="runtime", profile="gateway-dns-tcp-8000-v1")
    live = {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {"name": "runtime", "uid": "deployment-uid", "labels": runtime_labels},
        "spec": {"template": pod_template(runtime_labels)},
    }
    child = {
        "metadata": {
            "name": "runtime-rs",
            "labels": runtime_labels,
            "ownerReferences": [
                {
                    "apiVersion": "apps/v1",
                    "kind": "Deployment",
                    "name": "runtime",
                    "uid": "deployment-uid",
                    "controller": True,
                }
            ],
        },
        "spec": {"template": pod_template(runtime_labels)},
    }
    tampered = deepcopy(child)
    tampered["metadata"]["ownerReferences"][0][field] = "spoofed"
    reader = FakeReader({"apis/apps/v1/namespaces/fs2-models/deployments/runtime": live})
    with pytest.raises(NetworkBoundaryError):
        await admission(reader).review(
            review(
                kind="ReplicaSet",
                resource="replicasets",
                value=tampered,
                username=MANAGER,
            )
        )


@pytest.mark.asyncio
async def test_pod_profile_must_equal_live_parent_and_template() -> None:
    runtime_labels = labels(workload_class="runtime", profile="gateway-dns-tcp-8000-v1")
    parent = {
        "apiVersion": "apps/v1",
        "kind": "ReplicaSet",
        "metadata": {"name": "runtime-rs", "uid": "rs-uid", "labels": runtime_labels},
        "spec": {"template": pod_template(runtime_labels)},
    }
    pod = {
        "metadata": {
            "name": "runtime-pod",
            "labels": labels(workload_class="runtime", profile="job-public-acquisition-v1"),
            "ownerReferences": [
                {
                    "apiVersion": "apps/v1",
                    "kind": "ReplicaSet",
                    "name": "runtime-rs",
                    "uid": "rs-uid",
                    "controller": True,
                }
            ],
        },
        "spec": secure_pod_spec(),
    }
    reader = FakeReader({"apis/apps/v1/namespaces/fs2-models/replicasets/runtime-rs": parent})
    with pytest.raises(NetworkBoundaryError, match="differs from its live parent"):
        await admission(reader).review(review(kind="Pod", resource="pods", value=pod, username=MANAGER))


@pytest.mark.asyncio
async def test_protected_mutation_requires_live_random_holder_token() -> None:
    holder = "testrun:123:0123456789abcdef0123456789abcdef"
    reader = FakeReader(
        {
            "api/v1/namespaces/fs2-models/configmaps/fs2-runtime-network-policy-boundary-v2": {
                "metadata": {"name": "fs2-runtime-network-policy-boundary-v2"}
            },
            "apis/coordination.k8s.io/v1/namespaces/fs2-system/leases/fs2-model-network-transition": {
                "metadata": {
                    "annotations": {
                        "fs2-serve.nebius.ai/network-transition-writer": "fs2-model-network-transition",
                        "fs2-serve.nebius.ai/network-transition-holder": holder,
                    }
                },
                "spec": {
                    "holderIdentity": holder,
                    "leaseDurationSeconds": 7200,
                    "renewTime": "2999-01-01T00:00:00Z",
                },
            },
        }
    )
    policy = {
        "metadata": {
            "name": "fs2-runtime-profile-test",
            "namespace": "fs2-models",
            "annotations": {"fs2-serve.nebius.ai/network-transition-holder": "spoofed"},
        }
    }
    with pytest.raises(NetworkBoundaryError, match="random transition holder"):
        await admission(reader).review(
            review(
                kind="NetworkPolicy",
                resource="networkpolicies",
                value=policy,
                username="fs2-model-network-transition",
                group="networking.k8s.io",
            )
        )


@pytest.mark.asyncio
async def test_expired_transition_holder_can_be_replaced_but_active_holder_cannot() -> None:
    old_holder = "testrun:123:0123456789abcdef0123456789abcdef"
    new_holder = "testrun:456:fedcba9876543210fedcba9876543210"
    old = {
        "metadata": {
            "name": "fs2-model-network-transition",
            "annotations": {
                "fs2-serve.nebius.ai/network-transition-writer": "fs2-model-network-transition",
                "fs2-serve.nebius.ai/network-transition-holder": old_holder,
            },
        },
        "spec": {
            "holderIdentity": old_holder,
            "leaseDurationSeconds": 60,
            "renewTime": "2026-09-17T00:00:00Z",
        },
    }
    replacement = deepcopy(old)
    replacement["metadata"]["annotations"]["fs2-serve.nebius.ai/network-transition-holder"] = new_holder
    replacement["spec"].update(
        {
            "holderIdentity": new_holder,
            "renewTime": "2026-09-17T01:00:00Z",
        }
    )
    request = review(
        kind="Lease",
        resource="leases",
        value=replacement,
        old_value=old,
        operation="UPDATE",
        username="fs2-model-network-transition",
        group="coordination.k8s.io",
        namespace="fs2-system",
    )
    fixed_now = datetime(2026, 9, 17, 0, 2, tzinfo=UTC)
    result = await admission(FakeReader(), now=fixed_now).review(request)
    assert result["response"]["allowed"] is True

    active = deepcopy(old)
    active["spec"]["renewTime"] = "2026-09-17T00:01:30Z"
    request["request"]["oldObject"] = active
    with pytest.raises(NetworkBoundaryError, match="active transition holder"):
        await admission(FakeReader(), now=fixed_now).review(request)


@pytest.mark.asyncio
async def test_expired_transition_holder_cannot_authorize_protected_mutation() -> None:
    holder = "testrun:123:0123456789abcdef0123456789abcdef"
    reader = FakeReader(
        {
            "apis/coordination.k8s.io/v1/namespaces/fs2-system/leases/fs2-model-network-transition": {
                "metadata": {
                    "annotations": {
                        "fs2-serve.nebius.ai/network-transition-writer": "fs2-model-network-transition",
                        "fs2-serve.nebius.ai/network-transition-holder": holder,
                    }
                },
                "spec": {
                    "holderIdentity": holder,
                    "leaseDurationSeconds": 60,
                    "renewTime": "2026-09-17T00:00:00Z",
                },
            }
        }
    )
    policy = {
        "metadata": {
            "name": "fs2-runtime-profile-test",
            "annotations": {"fs2-serve.nebius.ai/network-transition-holder": holder},
        }
    }
    with pytest.raises(NetworkBoundaryError, match="expired transition holder"):
        await admission(reader, now=datetime(2026, 9, 17, 0, 2, tzinfo=UTC)).review(
            review(
                kind="NetworkPolicy",
                resource="networkpolicies",
                value=policy,
                username="fs2-model-network-transition",
                group="networking.k8s.io",
            )
        )


@pytest.mark.asyncio
async def test_expired_transition_holder_cannot_authorize_profiled_parent() -> None:
    holder = "testrun:123:0123456789abcdef0123456789abcdef"
    runtime_labels = labels(workload_class="runtime", profile="gateway-dns-tcp-8000-v1")
    reader = FakeReader(
        {
            "apis/coordination.k8s.io/v1/namespaces/fs2-system/leases/fs2-model-network-transition": {
                "metadata": {
                    "annotations": {
                        "fs2-serve.nebius.ai/network-transition-writer": "fs2-model-network-transition",
                        "fs2-serve.nebius.ai/network-transition-holder": holder,
                    }
                },
                "spec": {
                    "holderIdentity": holder,
                    "leaseDurationSeconds": 60,
                    "renewTime": "2026-09-17T00:00:00Z",
                },
            }
        }
    )
    deployment = {
        "metadata": {"name": "runtime", "labels": runtime_labels},
        "spec": {"template": pod_template(runtime_labels)},
    }
    with pytest.raises(NetworkBoundaryError, match="expired transition holder"):
        await admission(reader, now=datetime(2026, 9, 17, 0, 2, tzinfo=UTC)).review(
            review(
                kind="Deployment",
                resource="deployments",
                value=deployment,
                username="fs2-model-network-transition",
                group="apps",
            )
        )


@pytest.mark.asyncio
async def test_control_plane_release_is_permanently_frozen_when_transition_is_idle() -> None:
    reader = FakeReader(
        {
            "apis/coordination.k8s.io/v1/namespaces/fs2-system/leases/fs2-model-network-transition": {
                "metadata": {
                    "annotations": {"fs2-serve.nebius.ai/network-transition-writer": "fs2-model-network-transition"}
                },
                "spec": {
                    "holderIdentity": "",
                    "leaseDurationSeconds": 1,
                    "leaseTransitions": 7,
                },
            }
        }
    )
    deployment = {
        "metadata": {
            "name": "fs2-serve-control-plane-gateway",
            "labels": {"app.kubernetes.io/instance": "fs2-serve-control-plane"},
        }
    }
    with pytest.raises(NetworkBoundaryError, match="frozen outside"):
        await admission(reader).review(
            review(
                kind="Deployment",
                resource="deployments",
                value=deployment,
                username="cluster-admin@example.test",
                group="apps",
                namespace="fs2-system",
            )
        )


@pytest.mark.asyncio
async def test_dedicated_transition_writer_can_change_helm_only_after_deny_absent() -> None:
    holder = "testrun:123:0123456789abcdef0123456789abcdef"
    base_objects = {
        "api/v1/namespaces/fs2-models/configmaps/fs2-runtime-network-policy-boundary-v2": {
            "metadata": {"name": "fs2-runtime-network-policy-boundary-v2"}
        },
        "apis/coordination.k8s.io/v1/namespaces/fs2-system/leases/fs2-model-network-transition": {
            "metadata": {
                "annotations": {
                    "fs2-serve.nebius.ai/network-transition-writer": "fs2-model-network-transition",
                    "fs2-serve.nebius.ai/network-transition-holder": holder,
                }
            },
            "spec": {
                "holderIdentity": holder,
                "leaseDurationSeconds": 7200,
                "renewTime": "2026-09-17T00:00:00Z",
            },
        },
    }
    release = {
        "metadata": {
            "name": "sh.helm.release.v1.fs2-serve-control-plane.v135",
            "labels": {"owner": "helm", "name": "fs2-serve-control-plane"},
        }
    }
    request = review(
        kind="Secret",
        resource="secrets",
        value=release,
        username="fs2-model-network-transition",
        namespace="fs2-system",
    )
    inventory = (
        ReleaseInventoryEntry(
            group="",
            resource="secrets",
            namespace="fs2-system",
            name="sh.helm.release.v1.fs2-serve-control-plane.v135",
            operations=frozenset({"CREATE"}),
            object_sha256=NetworkBoundaryAdmission._release_semantic_sha256(release),
        ),
    )
    now = datetime(2026, 9, 17, 0, 30, tzinfo=UTC)
    result = await admission(
        FakeReader(base_objects), now=now, release_inventory=inventory
    ).review(request)
    assert result["response"]["allowed"] is True

    with_deny = deepcopy(base_objects)
    with_deny["apis/networking.k8s.io/v1/namespaces/fs2-models/networkpolicies/default-deny"] = {
        "metadata": {"name": "default-deny"}
    }
    with pytest.raises(NetworkBoundaryError):
        await admission(
            FakeReader(with_deny), now=now, release_inventory=inventory
        ).review(request)


@pytest.mark.asyncio
async def test_signed_release_identity_cannot_be_bypassed_by_labels_or_another_writer() -> None:
    release = {
        "metadata": {
            "name": "fs2-serve-control-plane-api",
            "labels": {},
        },
        "spec": {"replicas": 2},
    }
    inventory = (
        ReleaseInventoryEntry(
            group="apps",
            resource="deployments",
            namespace="fs2-system",
            name="fs2-serve-control-plane-api",
            operations=frozenset({"CREATE"}),
            object_sha256=NetworkBoundaryAdmission._release_semantic_sha256(release),
        ),
    )
    request = review(
        kind="Deployment",
        resource="deployments",
        value={**release, "spec": {"replicas": 99}},
        username="unrelated-writer",
        group="apps",
        namespace="fs2-system",
    )
    with pytest.raises(NetworkBoundaryError, match="finite Helm release inventory"):
        await admission(FakeReader(), release_inventory=inventory).review(request)
