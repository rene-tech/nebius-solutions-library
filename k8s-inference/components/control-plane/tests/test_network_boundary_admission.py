from __future__ import annotations

from copy import deepcopy
from typing import Any

import pytest

from fs2_serve.network_boundary_admission import (
    NetworkBoundaryAdmission,
    NetworkBoundaryConfig,
    NetworkBoundaryError,
)

PROFILE = "job-public-acquisition-v1"
CLASS = "public-acquisition"
MANAGER = "system:kube-controller-manager"


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


def admission(reader: FakeReader) -> NetworkBoundaryAdmission:
    return NetworkBoundaryAdmission(
        config=NetworkBoundaryConfig(
            model_namespace="fs2-models",
            system_namespace="fs2-system",
            acquisition_writer=("system:serviceaccount:fs2-system:fs2-serve-control-plane-runtime"),
            direct_job_writer=("system:serviceaccount:fs2-system:fs2-serve-control-plane-runtime"),
            jobset_writer="system:serviceaccount:jobset-system:jobset-controller",
            authorizer_writer="fs2-model-network-authorizer",
            transition_writer="fs2-model-network-transition",
        ),
        reader=reader,  # type: ignore[arg-type]
    )


def labels(*, workload_class: str = CLASS, profile: str = PROFILE) -> dict[str, str]:
    return {
        "app.kubernetes.io/part-of": "fs2-serve",
        "fs2-serve.nebius.ai/network-workload-class": workload_class,
        "fs2-serve.nebius.ai/network-profile": profile,
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
) -> dict[str, Any]:
    return {
        "request": {
            "uid": "request-uid",
            "operation": operation,
            "resource": {
                "group": group if group is not None else ("batch" if kind == "Job" else ""),
                "resource": resource,
            },
            "kind": {"kind": kind},
            "namespace": "fs2-models",
            "userInfo": {"username": username},
            "object": value,
            "oldObject": old_value,
        }
    }


@pytest.mark.asyncio
async def test_direct_public_acquisition_job_requires_normal_catalog_writer() -> None:
    job = {
        "metadata": {"name": "acquire", "labels": labels()},
        "spec": {"template": {"metadata": {"labels": labels()}}},
    }
    writer = "system:serviceaccount:fs2-system:fs2-serve-control-plane-runtime"
    result = await admission(FakeReader()).review(review(kind="Job", resource="jobs", value=job, username=writer))
    assert result["response"]["allowed"] is True

    with pytest.raises(NetworkBoundaryError, match="exact platform writer"):
        await admission(FakeReader()).review(
            review(
                kind="Job",
                resource="jobs",
                value=job,
                username="fs2-model-network-transition",
            )
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["apiVersion", "name", "uid"])
async def test_replicaset_child_is_bound_to_exact_live_deployment(field: str) -> None:
    runtime_labels = labels(workload_class="runtime", profile="gateway-dns-tcp-8000-v1")
    live = {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {"name": "runtime", "uid": "deployment-uid", "labels": runtime_labels},
        "spec": {"template": {"metadata": {"labels": runtime_labels}}},
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
        "spec": {"template": {"metadata": {"labels": runtime_labels}}},
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
        "spec": {"template": {"metadata": {"labels": runtime_labels}}},
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
        }
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
                "spec": {"holderIdentity": holder},
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
