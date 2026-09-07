"""Per-node feasibility regressions from the bounded customer trial."""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import httpx
import pytest
from test_scientific_batch_production import Fence, _workload_spec, profile_value, scheduling
from test_scientific_podset_envelope import _production_renderer

from fs2_serve.scientific_batch.kubernetes import HttpScientificBatchCluster, ScientificKubernetesError
from fs2_serve.scientific_batch.models import (
    ScientificBatchPlan,
    ScientificStagePlan,
    StagePlacementClass,
    StageResourceEnvelope,
    WorkloadKind,
)
from fs2_serve.scientific_batch.placement import (
    NODE_CAPACITY_SCHEMA,
    AcceleratorPodPlacement,
    PodPlacementError,
    execution_resource_envelope,
    stage_pod_request,
)
from fs2_serve.scientific_batch.podset_envelope import envelope_from_manifest
from fs2_serve.scientific_batch.scheduling import SchedulingContractError, SchedulingContractResolver

MIB = 1024**2
GIB = 1024**3


def capacity_contract() -> dict[str, Any]:
    value = copy.deepcopy(scheduling().contract)
    value["model_eligible_pool_ids"]["protein-design"] = ["h100-hot", "h100-preemptible"]
    value["accelerator_node_capacity_schema"] = NODE_CAPACITY_SCHEMA
    value["accelerator_node_capacity"] = {
        "h100-hot": {"cpu_millicores": 255900, "memory_mib": 2000000, "accelerator_count": 8},
        "h100-preemptible": {"cpu_millicores": 15900, "memory_mib": 65536, "accelerator_count": 1},
    }
    return value


def stage_resources(cpu: int = 16000, memory: int = 32 * GIB) -> StageResourceEnvelope:
    return StageResourceEnvelope(
        cpu_millis=cpu,
        memory_bytes=memory,
        ephemeral_storage_bytes=100 * GIB,
        limit_cpu_millis=cpu,
        limit_memory_bytes=memory,
        limit_ephemeral_storage_bytes=100 * GIB,
    )


def freeze(contract: dict[str, Any], *, cpu: int = 16000, memory: int = 32 * GIB, gpus: int = 1):
    profile = profile_value()
    profile["resources"]["gpu_count"] = gpus
    return SchedulingContractResolver(contract).freeze(
        service_class="customer-batch",
        model_id="protein-design",
        tenant_id="tenant-a",
        profile=profile,
        plan=ScientificBatchPlan(
            (
                ScientificStagePlan(
                    stage_id="design",
                    placement_class=StagePlacementClass.ACCELERATOR,
                    resources=stage_resources(cpu, memory),
                ),
            )
        ),
    )


def test_bindcraft_full_pod_excludes_small_pool_without_lowering_stage_request() -> None:
    snapshot = freeze(capacity_contract())
    assert snapshot.stages[0].resolved_pool_preference == ("h100-hot",)
    request = stage_pod_request(stage_resources(), accelerator_resource="nvidia.com/gpu", accelerator_count=1)
    assert request.cpu_millis == 16100
    assert request.memory_bytes == 32 * GIB + 256 * MIB


@pytest.mark.parametrize(
    "cpu,memory,gpus,expected",
    [
        (15800, 32 * GIB, 1, ("h100-hot", "h100-preemptible")),
        (15801, 32 * GIB, 1, ("h100-hot",)),
        (4000, 65536 * MIB, 1, ("h100-hot",)),
        (4000, (65536 - 256) * MIB, 1, ("h100-hot", "h100-preemptible")),
        (4000, 32 * GIB, 2, ("h100-hot",)),
    ],
)
def test_resource_boundaries_are_per_node(cpu, memory, gpus, expected) -> None:
    assert freeze(capacity_contract(), cpu=cpu, memory=memory, gpus=gpus).stages[0].resolved_pool_preference == expected


def test_aggregate_pool_quota_never_substitutes_for_node_fit() -> None:
    contract = capacity_contract()
    contract["core_capacity"] = {"h100-preemptible": {"cpu_millicores": 31800, "memory_mib": 131072}}
    assert freeze(contract).stages[0].resolved_pool_preference == ("h100-hot",)


def test_no_fitting_pool_is_rejected_before_durable_admission() -> None:
    with pytest.raises(SchedulingContractError, match="whole Pod requests 300100m CPU"):
        freeze(capacity_contract(), cpu=300000)


def test_unknown_capacity_excludes_only_unknown_pool() -> None:
    contract = capacity_contract()
    del contract["accelerator_node_capacity"]["h100-preemptible"]
    assert freeze(contract, cpu=4000).stages[0].resolved_pool_preference == ("h100-hot",)


def test_legacy_contract_is_explicitly_unverified_not_inferred_from_quota() -> None:
    legacy = scheduling()
    assert legacy.pod_placement.legacy_unverified
    assert legacy.pod_placement.capacities == {}


def test_legacy_profile_resources_use_qualified_execution_map_without_profile_mutation() -> None:
    profile = profile_value()
    original = copy.deepcopy(profile)
    resolver = SchedulingContractResolver(
        capacity_contract(),
        stage_resources={("protein-design", "design"): stage_resources()},
    )
    snapshot = resolver.freeze(
        service_class="customer-batch",
        model_id="protein-design",
        tenant_id="tenant-a",
        profile=profile,
        plan=ScientificBatchPlan((ScientificStagePlan(stage_id="design"),)),
    )
    assert snapshot.stages[0].resolved_pool_preference == ("h100-hot",)
    assert profile == original


def test_optional_ephemeral_capacity_is_checked_when_declared() -> None:
    contract = capacity_contract()
    contract["accelerator_node_capacity"]["h100-preemptible"]["ephemeral_storage_mib"] = 99 * 1024
    assert freeze(contract, cpu=4000).stages[0].resolved_pool_preference == ("h100-hot",)


def test_actual_renderer_companions_match_admission_request(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    renderer, resource = _production_renderer(tmp_path, monkeypatch)
    actual = envelope_from_manifest(renderer.render(resource), WorkloadKind.JOB).pod_sets[0].per_replica_requests
    resources = execution_resource_envelope(renderer.executions[("protein-design", "design")])
    planned = stage_pod_request(resources, accelerator_resource="nvidia.com/gpu", accelerator_count=1)
    assert actual == planned


@pytest.mark.parametrize("mutation", ["collector", "init", "native-sidecar", "overhead"])
def test_rendered_whole_pod_rechecks_regular_init_sidecars_and_overhead(mutation: str) -> None:
    manifest = {"spec": _workload_spec(WorkloadKind.JOB, cpu="15", memory="32Gi")}
    pod = manifest["spec"]["template"]["spec"]
    if mutation == "collector":
        pod["containers"][1]["resources"]["requests"]["cpu"] = "1"
    elif mutation == "init":
        pod["initContainers"].append(
            {"name": "large-init", "resources": {"requests": {"memory": "65Gi"}, "limits": {"memory": "65Gi"}}}
        )
    elif mutation == "native-sidecar":
        pod["initContainers"].append(
            {"name": "native-sidecar", "restartPolicy": "Always", "resources": {"requests": {"cpu": "1"}}}
        )
    else:
        pod["overhead"] = {"cpu": "1"}
    envelope = envelope_from_manifest(manifest, WorkloadKind.JOB)
    with pytest.raises(PodPlacementError, match="per-node capacity"):
        AcceleratorPodPlacement(capacity_contract()).validate_rendered(
            envelope, ("h100-preemptible",), "nvidia.com/gpu"
        )


def test_gang_fit_uses_per_replica_not_aggregate() -> None:
    manifest = {"spec": _workload_spec(WorkloadKind.JOB_SET, cpu="4", gang_size=8)}
    envelope = envelope_from_manifest(manifest, WorkloadKind.JOB_SET)
    assert envelope.pod_sets[0].aggregate_requests.cpu_millis > 15900
    AcceleratorPodPlacement(capacity_contract()).validate_rendered(envelope, ("h100-preemptible",), "nvidia.com/gpu")


@pytest.mark.asyncio
async def test_creation_refuses_manifest_drift_before_any_kubernetes_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    renderer, resource = _production_renderer(tmp_path, monkeypatch)
    contract = capacity_contract()
    contract["accelerator_node_capacity"]["h100-preemptible"]["cpu_millicores"] = 4000
    requests = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        raise AssertionError("infeasible Pod must never reach Kubernetes")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://kubernetes.test")
    cluster = HttpScientificBatchCluster(
        base_url="https://kubernetes.test",
        token_file=tmp_path / "unused",
        ca_file=tmp_path / "unused-ca",
        controller_id="controller-test",
        fence=Fence(),
        renderer=renderer,
        writes_enabled=True,
        pod_placement=AcceleratorPodPlacement(contract),
        client=client,
    )
    try:
        with pytest.raises(ScientificKubernetesError, match="per-node capacity"):
            cluster._prepare(resource, controller_fence=1)
        assert requests == []
    finally:
        await client.aclose()
