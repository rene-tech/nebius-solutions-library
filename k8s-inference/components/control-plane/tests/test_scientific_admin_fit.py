"""Current node upper bounds are negative evidence, never free capacity."""

from copy import deepcopy
from datetime import UTC, datetime

import pytest
from test_scientific_admin import OPERATION_ID, ModelAdapter, RunAdapter, _context
from test_scientific_admin_postgres import _state

from fs2_serve.admin import AdminProblemError
from fs2_serve.scientific_admin import ScientificAdminReadService
from fs2_serve.scientific_admin_fit import ScientificNodeFitAdapter, project_node_upper_bounds
from fs2_serve.scientific_admin_models import ScientificPlacementConstraints
from fs2_serve.scientific_admin_postgres import _stages

NOW = datetime(2026, 9, 19, 13, 30, tzinfo=UTC)


def placement(**updates):
    return ScientificPlacementConstraints(
        scheduling_digest="sha256:" + "a" * 64, eligible_pool_ids=["h100-1x"], namespace="fs2-models",
        required_node_labels={"accelerator.fs2.nebius/class": "h100"},
        pod_cpu_millis=16100, pod_memory_bytes=16 * 1024**3, pod_ephemeral_storage_bytes=10 * 1024**3,
        accelerator_count=1, accelerator_resource_name="nvidia.com/gpu",
    ).model_copy(update=updates)


def node(*, cpu="15900m", memory="30Gi", disk="50Gi", gpu="1", pool="h100-1x"):
    return {
        "metadata": {"labels": {"accelerator.fs2.nebius/pool-id": pool,
                                  "accelerator.fs2.nebius/class": "h100"}},
        "spec": {}, "status": {
            "conditions": [{"type": "Ready", "status": "True"}],
            "allocatable": {"cpu": cpu, "memory": memory, "ephemeral-storage": disk, "nvidia.com/gpu": gpu},
        },
    }


def result(nodes, **updates):
    return project_node_upper_bounds(placement(**updates), nodes, observed_at=NOW).pools[0]


def test_real_15900m_upper_bound_rejects_stage_plus_collector_despite_unreserved_gpu():
    value = result([node()])
    assert value.state == "blocked"
    assert value.max_allocatable_cpu_millis == 15900
    assert value.max_allocatable_accelerators == 1
    assert value.blocking_reasons == {"cpu_request_exceeds_node_allocatable": 1}


def test_passing_upper_bounds_never_claims_free_or_placeable_gpu():
    inventory = [node(cpu="32")]
    inventory[0]["spec"]["taints"] = [{"key": "dedicated", "effect": "NoSchedule"}]
    value = project_node_upper_bounds(placement(), inventory, observed_at=NOW)
    assert value.pools[0].state == "possible"
    assert "taints/tolerations" in value.reason and "may be occupied" in value.reason
    assert placement().live_fit == "not-observed"


@pytest.mark.parametrize("updates,reason", [
    ({"memory": "8Gi"}, "memory_request_exceeds_node_allocatable"),
    ({"disk": "1Gi"}, "ephemeral_storage_request_exceeds_node_allocatable"),
    ({"gpu": "0"}, "accelerator_request_exceeds_node_allocatable"),
])
def test_resource_specific_negative_reasons(updates, reason):
    value = result([node(cpu="32", **updates)])
    assert value.state == "blocked" and value.blocking_reasons == {reason: 1}


def test_pool_and_reference_labels_are_actual_renderer_constraints():
    nodes = [node(cpu="32", pool="other"), node(cpu="32")]
    value = result(nodes, required_node_labels={"storage.fs2.nebius/reference-data": "true"})
    assert value.nodes_observed == 1
    assert value.blocking_reasons == {"required_node_label_mismatch": 1}
    assert result([node(pool="other")]).blocking_reasons == {"no_current_nodes_in_pool": 1}


def test_unknown_quantities_stay_unknown_and_resource_maxima_do_not_invent_a_node():
    incomplete = node(cpu="32")
    del incomplete["status"]["allocatable"]["memory"]
    assert result([incomplete]).state == "unknown"
    assert result([incomplete]).max_allocatable_memory_bytes is None
    assert result([node(cpu="32")], pod_cpu_millis=None).state == "unknown"
    value = result([node(cpu="32", memory="1Gi"), node(cpu="1", memory="100Gi")])
    assert value.state == "blocked" and value.possible_nodes == 0
    assert value.max_allocatable_cpu_millis == 32000
    assert value.max_allocatable_memory_bytes == 100 * 1024**3


def test_cordoned_and_not_ready_nodes_are_not_possible():
    unready, cordoned = node(cpu="32"), node(cpu="32")
    unready["status"]["conditions"] = []
    cordoned["spec"]["unschedulable"] = True
    value = result([unready, cordoned])
    assert value.state == "blocked"
    assert value.blocking_reasons == {"node_not_ready": 1, "node_cordoned_or_deleting": 1}


@pytest.mark.asyncio
async def test_one_existing_reader_inventory_for_all_stages_without_mutating_frozen_contract():
    class Reader:
        calls = []

        async def list(self, path):
            self.calls.append(path)
            return [node()]

    stages = _stages(_state(), ())
    stages[0] = stages[0].model_copy(update={"placement": placement()})
    before = deepcopy(stages)
    reader = Reader()
    enriched, observed_at = await ScientificNodeFitAdapter(reader, clock=lambda: NOW).enrich(stages * 2)
    assert reader.calls == ["/api/v1/nodes"]
    assert observed_at == NOW and len(enriched) == 2
    assert all(stage.placement.node_upper_bound_fit.pools[0].state == "blocked" for stage in enriched)
    assert stages == before


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [RuntimeError, TimeoutError, ValueError])
async def test_node_source_failure_keeps_authorized_run_available_and_does_not_expose_raw_error(error):
    class Reader:
        async def list(self, path):
            raise error("SENSITIVE_KUBE_ERROR")

    service = ScientificAdminReadService(
        runs=RunAdapter(), models=ModelAdapter(), placement=ScientificNodeFitAdapter(Reader()), clock=lambda: NOW,
    )
    response = await service.run_detail(_context(), OPERATION_ID, tenant_id="tenant-oncology")
    source = next(item for item in response.meta.sources if item.id == "scientific-node-fit")
    assert source.state == "unavailable"
    assert response.data.run.id == str(OPERATION_ID)
    assert "SENSITIVE_KUBE_ERROR" not in response.model_dump_json()


@pytest.mark.asyncio
async def test_fit_read_occurs_only_after_existing_run_tenant_authorization():
    class Reader:
        calls = 0

        async def list(self, path):
            self.calls += 1
            return [node()]

    reader = Reader()
    service = ScientificAdminReadService(
        runs=RunAdapter(), models=ModelAdapter(), placement=ScientificNodeFitAdapter(reader), clock=lambda: NOW,
    )
    with pytest.raises(AdminProblemError):
        await service.run_detail(_context(), OPERATION_ID, tenant_id="other-tenant")
    assert reader.calls == 0


@pytest.mark.asyncio
async def test_current_fit_source_and_frozen_run_data_are_composed_without_historical_relabeling():
    class Runs(RunAdapter):
        async def get_run(self, operation_id, *, tenant_id):
            snapshot = await super().get_run(operation_id, tenant_id=tenant_id)
            snapshot.data.stages[0].placement = placement()
            return snapshot

    class Reader:
        async def list(self, path):
            return [node()]

    service = ScientificAdminReadService(
        runs=Runs(), models=ModelAdapter(), placement=ScientificNodeFitAdapter(Reader(), clock=lambda: NOW),
        clock=lambda: NOW,
    )
    response = await service.run_detail(_context(), OPERATION_ID, tenant_id="tenant-oncology")
    fit = response.data.stages[0].placement.node_upper_bound_fit
    assert fit.observed_at == NOW and fit.pools[0].state == "blocked"
    assert "historical runs" in fit.reason
    source = next(item for item in response.meta.sources if item.id == "scientific-node-fit")
    assert source.state == "available" and source.observed_at == NOW
