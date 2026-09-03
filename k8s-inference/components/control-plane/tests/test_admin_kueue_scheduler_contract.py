"""The admin Kueue projection must match what the scheduler actually renders.

The producer is `stages/workloads/queue.tf`. It writes the stable accelerator
class into `ResourceFlavor.spec.nodeLabels["accelerator.fs2.nebius/class"]` and
the capacity type into the `fs2-serve.nebius.ai/capacity-type` annotation,
because Kueue bounds `spec.nodeLabels` to eight entries. It also renders
LocalQueues and Workloads into every scientific lane namespace, not only the
model namespace. These tests pin the consumer to that rendered shape so a
producer change cannot quietly degrade the console to "unknown".
"""

from __future__ import annotations

from typing import Any

import pytest
from test_admin_capacity_observability import (
    FIXED_NOW,
    FakeKubernetesReader,
    _metadata,
)

from fs2_serve.admin_adapters import (
    KubernetesCapacityAdminAdapter,
    KubernetesCapacityConfig,
    KubernetesResourceNotFoundError,
)
from fs2_serve.admin_models import AdminCapacityType

PREFIX = "/apis/kueue.x-k8s.io/v1beta2"
ACADEMIC = "fs2-academic-poc"
REFERENCE = "fs2-reference-data"


def rendered_resource_flavor(name: str, *, accelerator_class: str, capacity_type: str) -> dict[str, Any]:
    """Exactly the manifest shape emitted by `kubernetes_manifest.accelerator_flavor`."""

    return {
        "apiVersion": "kueue.x-k8s.io/v1beta2",
        "kind": "ResourceFlavor",
        "metadata": {
            "name": name,
            "labels": {"accelerator.fs2.nebius/pool-id": f"{name}-pool"},
            "annotations": {
                "fs2-serve.nebius.ai/accelerator-contract-sha256": "0" * 64,
                "fs2-serve.nebius.ai/capacity-type": capacity_type,
                "fs2-serve.nebius.ai/min-nodes": "2",
                "fs2-serve.nebius.ai/max-nodes": "8",
            },
        },
        "spec": {
            "nodeLabels": {
                "accelerator.fs2.nebius/class": accelerator_class,
                "accelerator.fs2.nebius/pool-id": f"{name}-pool",
            },
            "tolerations": [],
        },
    }


def local_queue(name: str, namespace: str) -> dict[str, Any]:
    return {
        "metadata": _metadata(name, namespace=namespace),
        "spec": {"clusterQueue": "fs2-shared"},
        "status": {"pendingWorkloads": 0, "admittedWorkloads": 1},
    }


def workload(name: str, namespace: str) -> dict[str, Any]:
    return {
        "metadata": _metadata(name, namespace=namespace),
        "spec": {"queueName": f"{namespace}-lane", "priority": 100},
        "status": {"conditions": [{"type": "Admitted", "status": "True"}]},
    }


def kueue_values(*, namespaces: tuple[str, ...]) -> dict[str, Any]:
    values: dict[str, Any] = {
        f"{PREFIX}/resourceflavors": [
            rendered_resource_flavor(
                "h100-capacity-block",
                accelerator_class="nvidia-h100-sxm5-80gb",
                capacity_type="regular",
            ),
            rendered_resource_flavor(
                "h100-elastic",
                accelerator_class="nvidia-h100-sxm5-80gb",
                capacity_type="preemptible",
            ),
        ],
        f"{PREFIX}/clusterqueues": [],
        f"{PREFIX}/cohorts": KubernetesResourceNotFoundError("fixture Cohort API absent"),
    }
    for namespace in namespaces:
        values[f"{PREFIX}/namespaces/{namespace}/localqueues"] = [local_queue(f"{namespace}-lane", namespace)]
        values[f"{PREFIX}/namespaces/{namespace}/workloads"] = [workload(f"{namespace}-run", namespace)]
    return values


async def kueue_projection(config: KubernetesCapacityConfig, values: dict[str, Any]) -> Any:
    adapter = KubernetesCapacityAdminAdapter(
        FakeKubernetesReader(values),
        config=config,
        clock=lambda: FIXED_NOW,
    )
    return await adapter._kueue()


async def test_rendered_resource_flavor_reports_exact_class_and_capacity_type() -> None:
    """The scheduler's class label and capacity annotation must both be read."""

    projection = await kueue_projection(
        KubernetesCapacityConfig(),
        kueue_values(namespaces=("fs2-models",)),
    )

    by_name = {flavor.name: flavor for flavor in projection.resource_flavors}
    assert set(by_name) == {"h100-capacity-block", "h100-elastic"}

    for flavor in by_name.values():
        assert flavor.gpu_class == "nvidia-h100-sxm5-80gb"
        assert flavor.capacity_type is not AdminCapacityType.UNKNOWN

    assert by_name["h100-capacity-block"].capacity_type is AdminCapacityType.REGULAR
    assert by_name["h100-elastic"].capacity_type is AdminCapacityType.PREEMPTIBLE


async def test_scientific_lane_queues_and_workloads_are_not_hidden() -> None:
    """Academic and reference-data lanes must appear alongside the model lane."""

    namespaces = ("fs2-models", ACADEMIC, REFERENCE)
    config = KubernetesCapacityConfig(queue_namespaces=(ACADEMIC, REFERENCE))
    assert config.resolved_queue_namespaces == namespaces

    projection = await kueue_projection(config, kueue_values(namespaces=namespaces))

    assert {queue.namespace for queue in projection.local_queues} == set(namespaces)
    assert {item.namespace for item in projection.workloads} == set(namespaces)


async def test_model_namespace_is_never_duplicated_when_configured_again() -> None:
    config = KubernetesCapacityConfig(queue_namespaces=("fs2-models", ACADEMIC))
    assert config.resolved_queue_namespaces == ("fs2-models", ACADEMIC)

    values = kueue_values(namespaces=("fs2-models", ACADEMIC))
    projection = await kueue_projection(config, values)

    names = [queue.name for queue in projection.local_queues]
    assert len(names) == len(set(names))


async def test_a_failing_lane_fails_the_projection_instead_of_omitting_it() -> None:
    """A partial queue set would understate contention; it must not be served."""

    values = kueue_values(namespaces=("fs2-models", ACADEMIC))
    values[f"{PREFIX}/namespaces/{ACADEMIC}/workloads"] = KubernetesResourceNotFoundError("lane unreadable")

    projection = await kueue_projection(
        KubernetesCapacityConfig(queue_namespaces=(ACADEMIC,)),
        values,
    )

    assert projection.state.value == "unavailable"
    assert projection.local_queues == []
    assert projection.workloads == []


@pytest.mark.parametrize("namespace", ["Fs2-Models", "fs2_models", "-bad", ""])
def test_invalid_queue_namespace_is_rejected(namespace: str) -> None:
    with pytest.raises(ValueError, match="queue namespace is invalid"):
        KubernetesCapacityConfig(queue_namespaces=(namespace,))
