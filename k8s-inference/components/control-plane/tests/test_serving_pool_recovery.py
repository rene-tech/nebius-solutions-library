from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from test_model_deployment import model_spec, renderer, reserved_and_preemptible_envelope
from test_model_deployment_controller import FakeApi, controller, fence, model_object

from fs2_serve.model_deployment import WORKLOAD_POOL_ANNOTATION, RenderContext
from fs2_serve.model_deployment_controller import ModelKey
from fs2_serve.serving_pool_recovery import serving_pool_order

NOW = datetime(2026, 10, 8, 13, tzinfo=UTC)
ORDER = ["preemptible-h100", "reserved-h100"]


def node(pool, status="True", age=600):
    return {
        "metadata": {"labels": {"accelerator.fs2.nebius/pool-id": pool}},
        "status": {
            "conditions": [
                {
                    "type": "Ready",
                    "status": status,
                    "reason": "NodeStatusUnknown" if status == "Unknown" else "KubeletReady",
                    "lastTransitionTime": (NOW - timedelta(seconds=age)).isoformat(),
                }
            ]
        },
    }


def order(nodes, observed=(), scheduled=False):
    return serving_pool_order(
        pools=list(reserved_and_preemptible_envelope().pools.values()),
        default_order=ORDER,
        observed_order=observed,
        nodes=nodes,
        has_scheduled_pods=scheduled,
        now=NOW,
    )


def test_dead_primary_uses_qualified_ready_alternative():
    assert order([node(ORDER[0], "Unknown"), node(ORDER[1])]) == ORDER[::-1]


@pytest.mark.parametrize(
    "nodes",
    [
        None,
        [],
        [node(ORDER[0], "Unknown")],
        [node(ORDER[0], "Unknown", 119), node(ORDER[1])],
        [node(ORDER[0], "False"), node(ORDER[1])],
        [node(ORDER[0]), node(ORDER[0], "Unknown"), node(ORDER[1])],
        [node(ORDER[0], "Unknown"), node(ORDER[1], "Unknown")],
        [node("unqualified-pool")],
    ],
)
def test_missing_stale_partial_or_scale_from_zero_evidence_does_not_failover(nodes):
    assert order(nodes) == ORDER


def test_scheduled_loading_running_and_draining_pods_are_not_replaced():
    assert order([node(ORDER[0], "Unknown"), node(ORDER[1])], scheduled=True) == ORDER


def test_preference_survives_restart_primary_return_and_missing_telemetry():
    assert order(None, observed=ORDER[::-1]) == ORDER[::-1]
    assert order([node(ref) for ref in ORDER], observed=ORDER[::-1]) == ORDER[::-1]
    assert order([node(ORDER[0]), node(ORDER[1], "Unknown")], observed=ORDER[::-1]) == ORDER


def cold_spec():
    base = model_spec()
    return base.model_copy(
        update={
            "placement": base.placement.model_copy(update={"pool_refs": ORDER}),
            "availability": base.availability.model_copy(update={"min_replicas": 0, "max_replicas": 2}),
        }
    )


def test_renderer_moves_burst_budget_without_changing_total_or_eligibility():
    infrastructure = reserved_and_preemptible_envelope()
    context = RenderContext(
        name="qwen-live",
        namespace="fs2-models",
        uid="cr-uid-1",
        generation=1,
        pool=infrastructure.pools[ORDER[0]],
        eligible_pools=list(infrastructure.pools.values()),
        burst_pool_order=ORDER[::-1],
        prometheus_server_address="http://prometheus:9090",
    )
    rendered = renderer().render(cold_spec(), context)
    scalers = [item.manifest for item in rendered.resources if item.kind == "ScaledObject"]
    assert len(scalers) == 1
    assert scalers[0]["metadata"]["annotations"][WORKLOAD_POOL_ANNOTATION] == ORDER[1]
    assert scalers[0]["spec"]["maxReplicaCount"] == 2
    assert scalers[0]["spec"]["minReplicaCount"] == 0
    with pytest.raises(ValueError, match="permutation"):
        RenderContext.model_validate({**context.model_dump(), "burst_pool_order": ["unqualified"]})


@pytest.mark.asyncio
async def test_controller_handoff_and_restart_keep_actual_fallback_status():
    model = model_object()
    model["spec"] = cold_spec().model_dump(mode="json", by_alias=True)

    class NodeApi(FakeApi):
        nodes = [node(ORDER[0], "Unknown"), node(ORDER[1])]

        async def list_pool_nodes(self):
            return self.nodes

    api = NodeApi(model)
    api.nodes = None
    subject = controller(api)
    subject.envelope = reserved_and_preemptible_envelope()
    key = ModelKey(namespace="fs2-models", name="qwen-live")
    for _ in range(8):
        await subject.reconcile(key, fence())
    assert api.model["status"]["admittedPoolRef"] == ORDER[0]
    api.nodes = [node(ORDER[0], "Unknown"), node(ORDER[1])]
    for _ in range(8):
        await subject.reconcile(key, fence())
    assert api.model["status"]["admittedPoolRef"] == ORDER[1]
    assert {item["poolRef"] for item in api.model["status"]["placements"]} == {ORDER[1]}
    identities = set(api.resources)
    api.nodes = None
    restarted = controller(api)
    restarted.envelope = subject.envelope
    await restarted.reconcile(key, fence())
    assert set(api.resources) == identities
    assert api.model["status"]["admittedPoolRef"] == ORDER[1]
