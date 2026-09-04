from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import pytest

from fs2_serve.admin import (
    AdminContextConfig,
    AdminObservabilityQueryTemplates,
    AdminReadService,
    CapacityAdminAdapter,
)
from fs2_serve.admin_adapters import (
    KubernetesCapacityAdminAdapter,
    KubernetesCapacityConfig,
    KubernetesResourceNotFoundError,
    ManagedNodeScalerPoolContract,
    ObservabilityLinkConfig,
    PrometheusObservabilityAdminAdapter,
    PrometheusObservabilityConfig,
)
from fs2_serve.admin_models import (
    AdminCapabilityHealth,
    AdminCapacitySnapshot,
    AdminContext,
    AdminContextOption,
    AdminSourceState,
    AdminValueState,
)
from fs2_serve.memory_store import MemoryStore

FIXED_NOW = datetime(2026, 8, 30, 12, 0, tzinfo=UTC)
GPU_CLASSES = ("H100", "H200", "B200", "B300", "GB300", "RTX PRO 6000 Blackwell")


def _metadata(name: str, *, namespace: str | None = None, labels: dict[str, str] | None = None) -> dict[str, Any]:
    value: dict[str, Any] = {"name": name, "labels": labels or {}}
    if namespace is not None:
        value["namespace"] = namespace
    return value


def _node(ordinal: int, gpu_class: str, capacity_type: str, gpu_count: int) -> dict[str, Any]:
    return {
        "metadata": {
            "name": f"fixture-node-{ordinal}",
            "labels": {
                "node.kubernetes.io/instance-type": f"fixture-gpu-{ordinal}",
                "capacity.fs2.nebius/pool": f"pool-{ordinal}",
                "capacity.fs2.nebius/type": capacity_type,
                "nebius.com/gpu-name": gpu_class,
            },
        },
        "spec": {"unschedulable": ordinal == 5},
        "status": {
            "capacity": {"nvidia.com/gpu": str(gpu_count)},
            "allocatable": {"nvidia.com/gpu": str(gpu_count)},
            "conditions": [{"type": "Ready", "status": "False" if ordinal == 5 else "True"}],
        },
    }


def _pod(ordinal: int) -> dict[str, Any]:
    return {
        "metadata": _metadata(f"fixture-pod-{ordinal}", namespace="fs2-models"),
        "spec": {
            "nodeName": f"fixture-node-{ordinal}",
            "containers": [
                {
                    "resources": {
                        "requests": {"nvidia.com/gpu": "1"},
                        "limits": {"nvidia.com/gpu": "1"},
                    }
                }
            ],
        },
        "status": {"phase": "Running"},
    }


class FakeKubernetesReader:
    def __init__(
        self,
        values: dict[str, list[dict[str, Any]] | dict[str, Any] | BaseException],
    ) -> None:
        self.values = values
        self.paths: list[str] = []

    async def list(self, path: str) -> list[dict[str, Any]]:
        self.paths.append(path)
        value = self.values[path]
        if isinstance(value, BaseException):
            raise value
        assert isinstance(value, list)
        return value

    async def get(self, path: str) -> dict[str, Any]:
        self.paths.append(path)
        value = self.values[path]
        if isinstance(value, BaseException):
            raise value
        assert isinstance(value, dict)
        return value


def _kubernetes_values() -> dict[str, list[dict[str, Any]] | dict[str, Any] | BaseException]:
    nodes = [
        _node(index, gpu_class, "preemptible" if index % 2 else "regular", 8 if index < 5 else 1)
        for index, gpu_class in enumerate(GPU_CLASSES)
    ]
    cluster_queue = {
        "metadata": _metadata("fixture-cluster-queue"),
        "spec": {
            "queueingStrategy": "BestEffortFIFO",
            "resourceGroups": [
                {
                    "flavors": [
                        {
                            "name": "fixture-preemptible",
                            "resources": [{"name": "nvidia.com/gpu", "nominalQuota": "16"}],
                        }
                    ]
                }
            ],
        },
        "status": {
            "pendingWorkloads": 0,
            "reservingWorkloads": 0,
            "admittedWorkloads": 0,
            "flavorsReservation": [
                {
                    "name": "fixture-preemptible",
                    "resources": [{"name": "nvidia.com/gpu", "total": "0", "borrowed": "0"}],
                }
            ],
            # flavorsUsage is intentionally absent: zero must not be invented.
            "conditions": [{"type": "Active", "status": "True"}],
        },
    }
    local_queue = {
        "metadata": _metadata("fixture-local-queue", namespace="fs2-models"),
        "spec": {"clusterQueue": "fixture-cluster-queue"},
        "status": {"pendingWorkloads": 0, "reservingWorkloads": 0, "admittedWorkloads": 0},
    }
    prefix = "/apis/kueue.x-k8s.io/v1beta2"
    return {
        "/api/v1/namespaces/kube-system/configmaps/cluster-autoscaler-status": {
            "metadata": _metadata("cluster-autoscaler-status", namespace="kube-system"),
            "data": {
                "status": (
                    "autoscalerStatus: Running\n"
                    "clusterWide:\n"
                    "  health:\n"
                    '    lastProbeTime: "2026-08-30T12:00:00Z"\n'
                    "    status: Healthy\n"
                    "  scaleDown:\n"
                    "    status: NoCandidates\n"
                    "nodeGroups: []\n"
                )
            },
        },
        "/api/v1/nodes": nodes,
        "/api/v1/namespaces/fs2-models/pods": [_pod(index) for index in range(len(nodes))],
        f"{prefix}/resourceflavors": [
            {
                "metadata": _metadata("fixture-preemptible"),
                "spec": {
                    "nodeLabels": {
                        "capacity.fs2.nebius/type": "preemptible",
                        "nebius.com/gpu-name": "H200",
                    }
                },
            }
        ],
        f"{prefix}/clusterqueues": [cluster_queue],
        f"{prefix}/namespaces/fs2-models/localqueues": [local_queue],
        f"{prefix}/namespaces/fs2-models/workloads": [],
        f"{prefix}/cohorts": KubernetesResourceNotFoundError("fixture Cohort API absent"),
        "/apis/autoscaling/v2/namespaces/fs2-models/horizontalpodautoscalers": [
            {
                "metadata": _metadata("fixture-model", namespace="fs2-models"),
                "spec": {
                    "scaleTargetRef": {"kind": "Deployment", "name": "fixture-model"},
                    "minReplicas": 0,
                    "maxReplicas": 8,
                },
                "status": {
                    "desiredReplicas": 0,
                    # currentReplicas is intentionally absent.
                    "conditions": [{"type": "AbleToScale", "status": "True"}],
                },
            }
        ],
        "/apis/autoscaling/v2/namespaces/fs2-system/horizontalpodautoscalers": [],
        "/apis/keda.sh/v1alpha1/namespaces/fs2-models/scaledobjects": [],
    }


@pytest.mark.asyncio
async def test_mixed_gpu_capacity_is_dynamic_estimated_and_never_infers_health() -> None:
    reader = FakeKubernetesReader(_kubernetes_values())
    snapshot = await KubernetesCapacityAdminAdapter(
        reader,
        config=KubernetesCapacityConfig(),
        clock=lambda: FIXED_NOW,
    ).snapshot()

    pools = snapshot.data.node_pools
    assert pools.state == AdminSourceState.AVAILABLE
    assert {item.gpu_class for item in pools.items} == set(GPU_CLASSES)
    assert {item.capacity_type.value for item in pools.items} == {"regular", "preemptible"}
    for pool in pools.items:
        gpu = pool.gpu_resources[0]
        assert gpu.resource_name == "nvidia.com/gpu"
        assert gpu.allocated.value == 1
        assert gpu.allocated.state == AdminValueState.ESTIMATED
        assert "Pod requests" in (gpu.allocated.reason or "")
        assert gpu.healthy.value is None
        assert gpu.healthy.state == AdminValueState.UNAVAILABLE
        assert "health evidence" in (gpu.healthy.reason or "")
        assert "fixture-node" not in pool.model_dump_json()

    quota = snapshot.data.kueue.cluster_queues[0].resources[0]
    assert quota.nominal_quota.value == "16"
    assert quota.reservation.value == "0"
    assert quota.borrowed.value == "0"
    assert quota.usage.value is None
    assert quota.usage.state == AdminValueState.UNAVAILABLE
    assert snapshot.data.kueue.cohorts_state == AdminSourceState.UNAVAILABLE
    assert snapshot.data.autoscaling.keda.state == AdminSourceState.AVAILABLE
    assert snapshot.data.autoscaling.keda.keda_scaled_objects == []
    hpa = snapshot.data.autoscaling.hpa.horizontal_pod_autoscalers[0]
    assert hpa.min_replicas.value == 0
    assert hpa.desired_replicas.value == 0
    assert hpa.current_replicas.value is None
    assert snapshot.data.node_scaler.state == AdminSourceState.UNAVAILABLE


@pytest.mark.asyncio
async def test_managed_node_scaler_uses_exact_pool_bounds_and_provider_pool_label_fallback() -> None:
    values = _kubernetes_values()
    nodes = values["/api/v1/nodes"]
    assert isinstance(nodes, list)
    labels = nodes[0]["metadata"]["labels"]
    labels.pop("capacity.fs2.nebius/pool")
    labels["accelerator.fs2.nebius/pool-id"] = "elastic-h100"

    snapshot = await KubernetesCapacityAdminAdapter(
        FakeKubernetesReader(values),
        config=KubernetesCapacityConfig(
            node_scaler_provider="nebius-managed-node-group-autoscaler",
            node_scaler_pools=(
                ManagedNodeScalerPoolContract(pool_id="elastic-h100", min_nodes=1, max_nodes=2),
                ManagedNodeScalerPoolContract(pool_id="scale-to-zero", min_nodes=0, max_nodes=4),
            ),
        ),
        clock=lambda: FIXED_NOW,
    ).snapshot()

    h100 = next(pool for pool in snapshot.data.node_pools.items if pool.gpu_class == "H100")
    assert h100.pool_label == "elastic-h100"
    scaler = snapshot.data.node_scaler
    assert scaler.state == AdminSourceState.AVAILABLE
    assert scaler.provider == "nebius-managed-node-group-autoscaler"
    assert scaler.configured is True
    assert scaler.healthy is True
    assert scaler.observed_at == FIXED_NOW
    assert scaler.reason is None


@pytest.mark.asyncio
async def test_managed_node_scaler_prefers_exact_pool_id_over_semantic_classification() -> None:
    values = _kubernetes_values()
    nodes = values["/api/v1/nodes"]
    assert isinstance(nodes, list)
    labels = nodes[0]["metadata"]["labels"]
    labels["capacity.fs2.nebius/pool"] = "burst"
    labels["accelerator.fs2.nebius/pool-id"] = "b300-preemptible-8x"

    snapshot = await KubernetesCapacityAdminAdapter(
        FakeKubernetesReader(values),
        config=KubernetesCapacityConfig(
            node_scaler_provider="nebius-managed-node-group-autoscaler",
            node_scaler_pools=(ManagedNodeScalerPoolContract(pool_id="b300-preemptible-8x", min_nodes=1, max_nodes=2),),
        ),
        clock=lambda: FIXED_NOW,
    ).snapshot()

    h100 = next(pool for pool in snapshot.data.node_pools.items if pool.gpu_class == "H100")
    assert h100.pool_label == "b300-preemptible-8x"
    assert snapshot.data.node_scaler.state == AdminSourceState.AVAILABLE
    assert snapshot.data.node_scaler.healthy is True


@pytest.mark.asyncio
async def test_managed_node_scaler_fails_closed_when_provider_probe_is_stale() -> None:
    values = _kubernetes_values()
    status = values["/api/v1/namespaces/kube-system/configmaps/cluster-autoscaler-status"]
    assert isinstance(status, dict)
    status["data"]["status"] = status["data"]["status"].replace("2026-08-30T12:00:00Z", "2026-08-30T11:00:00Z")

    snapshot = await KubernetesCapacityAdminAdapter(
        FakeKubernetesReader(values),
        config=KubernetesCapacityConfig(
            node_scaler_provider="nebius-managed-node-group-autoscaler",
            node_scaler_pools=(ManagedNodeScalerPoolContract(pool_id="pool-0", min_nodes=1, max_nodes=2),),
        ),
        clock=lambda: FIXED_NOW,
    ).snapshot()

    assert snapshot.data.node_scaler.state == AdminSourceState.UNAVAILABLE
    assert snapshot.data.node_scaler.healthy is None
    assert snapshot.data.node_scaler.reason == "managed cluster-autoscaler health evidence is stale"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("old", "new", "expected_reason"),
    [
        ("status: Healthy", "status: Unhealthy", "managed cluster-autoscaler reports Unhealthy"),
        ("autoscalerStatus: Running", "autoscalerStatus: Stopped", "managed cluster-autoscaler is not Running"),
    ],
)
async def test_managed_node_scaler_reports_provider_unhealthy_without_failing_the_snapshot(
    old: str,
    new: str,
    expected_reason: str,
) -> None:
    values = _kubernetes_values()
    status = values["/api/v1/namespaces/kube-system/configmaps/cluster-autoscaler-status"]
    assert isinstance(status, dict)
    status["data"]["status"] = status["data"]["status"].replace(old, new, 1)

    snapshot = await KubernetesCapacityAdminAdapter(
        FakeKubernetesReader(values),
        config=KubernetesCapacityConfig(
            node_scaler_provider="nebius-managed-node-group-autoscaler",
            node_scaler_pools=(ManagedNodeScalerPoolContract(pool_id="pool-0", min_nodes=1, max_nodes=2),),
        ),
        clock=lambda: FIXED_NOW,
    ).snapshot()

    assert snapshot.data.node_scaler.state == AdminSourceState.AVAILABLE
    assert snapshot.data.node_scaler.healthy is False
    assert snapshot.data.node_scaler.reason == expected_reason


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("reported_status", "expected_reason"),
    [
        ("Degraded", "managed cluster-autoscaler health state is unsupported"),
        ("A" * 256, "managed cluster-autoscaler health evidence is malformed"),
    ],
)
async def test_managed_node_scaler_rejects_unknown_or_unbounded_provider_health_state(
    reported_status: str,
    expected_reason: str,
) -> None:
    values = _kubernetes_values()
    status = values["/api/v1/namespaces/kube-system/configmaps/cluster-autoscaler-status"]
    assert isinstance(status, dict)
    status["data"]["status"] = status["data"]["status"].replace(
        "status: Healthy",
        f"status: {reported_status}",
        1,
    )

    snapshot = await KubernetesCapacityAdminAdapter(
        FakeKubernetesReader(values),
        config=KubernetesCapacityConfig(
            node_scaler_provider="nebius-managed-node-group-autoscaler",
            node_scaler_pools=(ManagedNodeScalerPoolContract(pool_id="pool-0", min_nodes=1, max_nodes=2),),
        ),
        clock=lambda: FIXED_NOW,
    ).snapshot()

    assert snapshot.data.node_scaler.state == AdminSourceState.UNAVAILABLE
    assert snapshot.data.node_scaler.healthy is None
    assert snapshot.data.node_scaler.reason == expected_reason
    assert reported_status not in (snapshot.data.node_scaler.reason or "")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("pool_id", "min_nodes", "max_nodes"),
    [
        ("pool-5", 1, 1),  # observed node is not Ready
        ("pool-0", 0, 0),  # observed count exceeds max_nodes
        ("missing-pool", 1, 2),  # observed count is below min_nodes
    ],
)
async def test_managed_node_scaler_is_available_but_unhealthy_outside_bounds_or_readiness(
    pool_id: str,
    min_nodes: int,
    max_nodes: int,
) -> None:
    snapshot = await KubernetesCapacityAdminAdapter(
        FakeKubernetesReader(_kubernetes_values()),
        config=KubernetesCapacityConfig(
            node_scaler_provider="nebius-managed-node-group-autoscaler",
            node_scaler_pools=(
                ManagedNodeScalerPoolContract(pool_id=pool_id, min_nodes=min_nodes, max_nodes=max_nodes),
            ),
        ),
        clock=lambda: FIXED_NOW,
    ).snapshot()

    scaler = snapshot.data.node_scaler
    assert scaler.state == AdminSourceState.AVAILABLE
    assert scaler.configured is True
    assert scaler.healthy is False
    assert scaler.observed_at == FIXED_NOW
    assert scaler.reason == "managed node-pool bounds or readiness are unhealthy"


@pytest.mark.asyncio
async def test_managed_node_scaler_is_honestly_unavailable_without_node_evidence() -> None:
    values = _kubernetes_values()
    values["/api/v1/nodes"] = RuntimeError("sensitive transport detail")
    snapshot = await KubernetesCapacityAdminAdapter(
        FakeKubernetesReader(values),
        config=KubernetesCapacityConfig(
            node_scaler_provider="nebius-managed-node-group-autoscaler",
            node_scaler_pools=(ManagedNodeScalerPoolContract(pool_id="pool-0", min_nodes=0, max_nodes=2),),
        ),
        clock=lambda: FIXED_NOW,
    ).snapshot()

    scaler = snapshot.data.node_scaler
    assert scaler.state == AdminSourceState.UNAVAILABLE
    assert scaler.provider == "nebius-managed-node-group-autoscaler"
    assert scaler.configured is True
    assert scaler.healthy is None
    assert scaler.observed_at is None
    assert "sensitive" not in (scaler.reason or "")


@pytest.mark.asyncio
async def test_pod_list_failure_preserves_capacity_but_allocation_is_unavailable() -> None:
    values = _kubernetes_values()
    values["/api/v1/namespaces/fs2-models/pods"] = RuntimeError("sensitive transport detail")
    snapshot = await KubernetesCapacityAdminAdapter(FakeKubernetesReader(values), clock=lambda: FIXED_NOW).snapshot()
    assert snapshot.data.node_pools.state == AdminSourceState.AVAILABLE
    assert all(
        resource.allocated.value is None for pool in snapshot.data.node_pools.items for resource in pool.gpu_resources
    )


@pytest.mark.asyncio
async def test_missing_node_capacity_field_is_unavailable_not_invented_zero() -> None:
    values = _kubernetes_values()
    nodes = values["/api/v1/nodes"]
    assert isinstance(nodes, list)
    nodes[0]["status"]["capacity"].pop("nvidia.com/gpu")

    snapshot = await KubernetesCapacityAdminAdapter(FakeKubernetesReader(values), clock=lambda: FIXED_NOW).snapshot()
    pool = next(item for item in snapshot.data.node_pools.items if item.gpu_class == "H100")
    gpu = pool.gpu_resources[0]
    assert gpu.capacity.state == AdminValueState.UNAVAILABLE
    assert gpu.capacity.value is None
    assert gpu.allocatable.state == AdminValueState.AVAILABLE
    assert gpu.allocatable.value == 8


class SlowCapacityAdapter(CapacityAdminAdapter):
    async def snapshot(self) -> AdminCapacitySnapshot:
        await asyncio.sleep(1)
        raise AssertionError("adapter timeout did not cancel the call")


@pytest.mark.asyncio
async def test_capacity_timeout_is_partial_unavailable_not_empty_success(
    registry: Any,
    cipher: Any,
    hasher: Any,
) -> None:
    service = AdminReadService(
        registry=registry,
        store=MemoryStore(cipher, hasher),
        capacity=SlowCapacityAdapter(),
        contexts=AdminContextConfig(
            options=(
                AdminContextOption(
                    project="project-test",
                    cluster="cluster-test",
                    region="region-test",
                    label="Test cluster",
                ),
            )
        ),
        clock=lambda: FIXED_NOW,
        adapter_timeout_seconds=0.1,
    )
    context = service.resolve_context(
        project=None,
        cluster=None,
        region=None,
        from_at=None,
        to_at=None,
        timezone="UTC",
    )
    result = await service.capacity(context)
    assert result.meta.sources[0].state == AdminSourceState.UNAVAILABLE
    assert result.data.node_pools.state == AdminSourceState.UNAVAILABLE
    assert result.data.node_pools.items == []
    assert result.data.node_pools.reason


@pytest.mark.asyncio
async def test_capacity_and_observability_staleness_fail_closed(
    registry: Any,
    cipher: Any,
    hasher: Any,
) -> None:
    stale_at = FIXED_NOW - timedelta(minutes=5)
    service = AdminReadService(
        registry=registry,
        store=MemoryStore(cipher, hasher),
        capacity=KubernetesCapacityAdminAdapter(
            FakeKubernetesReader(_kubernetes_values()),
            clock=lambda: stale_at,
        ),
        observability=PrometheusObservabilityAdminAdapter(
            FakePrometheusReader(),
            clock=lambda: stale_at,
        ),
        clock=lambda: FIXED_NOW,
        source_max_age_seconds=90,
    )
    context = service.resolve_context(
        project=None,
        cluster=None,
        region=None,
        from_at=None,
        to_at=None,
        timezone="UTC",
    )

    capacity = await service.capacity(context)
    assert capacity.meta.sources[0].state == AdminSourceState.STALE
    assert capacity.data.node_pools.state == AdminSourceState.STALE
    assert capacity.data.node_pools.items == []

    observability = await service.observability(context, model_id=None, operation_id=None)
    assert observability.meta.sources[0].state == AdminSourceState.STALE
    assert all(component.health == AdminCapabilityHealth.UNKNOWN for component in observability.data.components)
    assert all(component.launch.enabled is False for component in observability.data.components)


class FakePrometheusReader:
    def __init__(self, *, fail_component: str | None = None) -> None:
        self.fail_component = fail_component
        self.queries: list[str] = []

    async def scalar(self, query: str, *, at: datetime) -> float | None:
        assert at == FIXED_NOW
        self.queries.append(query)
        if self.fail_component and self.fail_component in query:
            raise RuntimeError("SENSITIVE_PROMETHEUS_DETAIL")
        if "rate(" in query:
            return 0
        if "grafana" in query or "prometheus" in query or "loki" in query or "otel" in query:
            return 1
        if "alertmanager" in query or "tempo" in query or "dcgm" in query.lower():
            return 0
        if "kueue" in query or "keda" in query:
            return 0
        if "DCGM_FI_" in query:
            return 0
        return 0


def _context() -> AdminContext:
    return AdminContext(
        project="project-test",
        cluster="cluster-test",
        region="region-test",
        from_at=FIXED_NOW - timedelta(hours=1),
        to_at=FIXED_NOW,
        timezone="UTC",
    )


@pytest.mark.asyncio
async def test_observability_preserves_four_way_state_and_sanitized_context_links() -> None:
    reader = FakePrometheusReader()
    links = ObservabilityLinkConfig(
        component_urls={
            "grafana": "https://observe.example.invalid/d/fs2",
            "prometheus": "https://observe.example.invalid/prometheus/graph",
            "loki": "https://observe.example.invalid/explore",
        },
        allowed_hosts=frozenset({"observe.example.invalid"}),
    )
    adapter = PrometheusObservabilityAdminAdapter(
        reader,
        config=PrometheusObservabilityConfig(
            links=links,
            installed_overrides={"alertmanager": False, "tempo": False, "dcgm": True},
            versions={"grafana": "13.2.0", "otel": "0.158.0"},
        ),
        clock=lambda: FIXED_NOW,
    )
    operation_id = uuid4()
    snapshot = await adapter.snapshot(context=_context(), model_id="qwen3-8b", operation_id=operation_id)
    components = {component.id: component for component in snapshot.data.components}

    assert components["grafana"].installed is True
    assert components["grafana"].health == AdminCapabilityHealth.HEALTHY
    assert components["grafana"].data_present is True
    assert components["grafana"].launch.enabled is True
    assert components["grafana"].launch.url is not None
    assert "var-model=qwen3-8b" in components["grafana"].launch.url
    assert f"var-operation={operation_id}" in components["grafana"].launch.url
    assert components["prometheus"].installed is True
    assert components["prometheus"].health == AdminCapabilityHealth.HEALTHY
    assert components["prometheus"].data_present is True
    assert components["prometheus"].launch.enabled is True
    assert components["dcgm"].installed is True
    assert components["dcgm"].health == AdminCapabilityHealth.UNKNOWN
    assert components["dcgm"].data_present is False
    assert components["dcgm"].launch.enabled is False
    assert components["alertmanager"].installed is False
    assert components["tempo"].installed is False
    assert snapshot.data.signals.gpu_utilization_ratio.value is None
    assert snapshot.data.signals.otel_refused_items_per_second.value == 0

    forbidden = ("tenant", "principal", "token", "operation_id", "model_id")
    assert all(not any(value in query.casefold() for value in forbidden) for query in reader.queries)


@pytest.mark.asyncio
async def test_one_observability_probe_failure_is_partial_and_sanitized() -> None:
    adapter = PrometheusObservabilityAdminAdapter(
        FakePrometheusReader(fail_component="kueue"),
        config=PrometheusObservabilityConfig(installed_overrides={"alertmanager": False, "tempo": False}),
        clock=lambda: FIXED_NOW,
    )
    snapshot = await adapter.snapshot(context=_context(), model_id=None, operation_id=None)
    components = {component.id: component for component in snapshot.data.components}
    assert components["grafana"].health == AdminCapabilityHealth.HEALTHY
    assert components["kueue"].health == AdminCapabilityHealth.UNKNOWN
    assert components["kueue"].data_present is None
    assert "SENSITIVE_PROMETHEUS_DETAIL" not in snapshot.model_dump_json()


@pytest.mark.asyncio
async def test_alertmanager_and_tempo_use_authenticated_grafana_surfaces() -> None:
    class HealthyOperatorReader(FakePrometheusReader):
        async def scalar(self, query: str, *, at: datetime) -> float | None:
            if "alertmanager" in query or "tempo" in query:
                assert at == FIXED_NOW
                self.queries.append(query)
                return 1
            return await super().scalar(query, at=at)

    operation_id = uuid4()
    adapter = PrometheusObservabilityAdminAdapter(
        HealthyOperatorReader(),
        config=PrometheusObservabilityConfig(
            links=ObservabilityLinkConfig(
                component_urls={
                    "alertmanager": "https://observe.example.invalid/admin/observability/grafana/alerting/silences",
                    "tempo": "https://observe.example.invalid/admin/observability/grafana/explore",
                },
                allowed_hosts=frozenset({"observe.example.invalid"}),
            ),
            installed_overrides={"alertmanager": True, "tempo": True},
            datasource_uids={
                "alertmanager": "fs2-r0123456789-alertmanager",
                "tempo": "fs2-r0123456789-tempo",
            },
        ),
        clock=lambda: FIXED_NOW,
    )

    snapshot = await adapter.snapshot(context=_context(), model_id="qwen3-8b", operation_id=operation_id)
    components = {component.id: component for component in snapshot.data.components}

    alertmanager_url = components["alertmanager"].launch.url
    assert components["alertmanager"].health == AdminCapabilityHealth.HEALTHY
    assert alertmanager_url is not None
    parsed_alertmanager = urlsplit(alertmanager_url)
    assert parsed_alertmanager.path.endswith("/grafana/alerting/silences")
    assert parse_qs(parsed_alertmanager.query)["alertmanager"] == ["fs2-r0123456789-alertmanager"]

    tempo_url = components["tempo"].launch.url
    assert components["tempo"].health == AdminCapabilityHealth.HEALTHY
    assert tempo_url is not None
    parsed = urlsplit(tempo_url)
    assert parsed.path.endswith("/grafana/explore")
    query = parse_qs(parsed.query)
    assert query["schemaVersion"] == ["1"]
    assert query["orgId"] == ["1"]
    assert set(query) == {"orgId", "panes", "schemaVersion"}
    pane = json.loads(query["panes"][0])["trace"]
    assert pane["datasource"] == "fs2-r0123456789-tempo"
    assert pane["queries"] == [
        {
            "datasource": {"type": "tempo", "uid": "fs2-r0123456789-tempo"},
            "limit": 20,
            "query": (f'{{ span."fs2.model.id" = "qwen3-8b" && span."fs2.operation.id" = "{operation_id}" }}'),
            "queryType": "traceql",
            "refId": "A",
            "tableType": "traces",
        }
    ]
    assert pane["range"] == {
        "from": str(int(_context().from_at.timestamp() * 1000)),
        "to": str(int(_context().to_at.timestamp() * 1000)),
    }


def test_datasource_backed_launch_requires_a_provisioned_identity() -> None:
    config = PrometheusObservabilityConfig(
        links=ObservabilityLinkConfig(
            component_urls={
                "alertmanager": "https://observe.example.invalid/admin/observability/grafana/alerting/silences",
                "tempo": "https://observe.example.invalid/admin/observability/grafana/explore",
            },
            allowed_hosts=frozenset({"observe.example.invalid"}),
        ),
        installed_overrides={"tempo": True},
    )
    assert config.links is not None
    assert (
        config.links.contextual_url(
            "tempo",
            datasource_uid=None,
            project="project-test",
            cluster="cluster-test",
            region="region-test",
            from_at=_context().from_at,
            to_at=_context().to_at,
            model_id=None,
            operation_id=None,
        )
        is None
    )
    assert (
        config.links.contextual_url(
            "alertmanager",
            datasource_uid=None,
            project="project-test",
            cluster="cluster-test",
            region="region-test",
            from_at=_context().from_at,
            to_at=_context().to_at,
            model_id=None,
            operation_id=None,
        )
        is None
    )


@pytest.mark.asyncio
async def test_all_prometheus_probes_failing_is_unknown_and_never_launchable() -> None:
    class FailingPrometheusReader:
        async def scalar(self, query: str, *, at: datetime) -> float | None:
            raise RuntimeError("SENSITIVE_ALL_PROBES_FAILED")

    links = ObservabilityLinkConfig(
        component_urls={"prometheus": "https://observe.example.invalid/prometheus/graph"},
        allowed_hosts=frozenset({"observe.example.invalid"}),
    )
    adapter = PrometheusObservabilityAdminAdapter(
        FailingPrometheusReader(),
        config=PrometheusObservabilityConfig(links=links),
        clock=lambda: FIXED_NOW,
    )
    snapshot = await adapter.snapshot(context=_context(), model_id=None, operation_id=None)
    components = {component.id: component for component in snapshot.data.components}

    assert all(component.health == AdminCapabilityHealth.UNKNOWN for component in components.values())
    assert all(component.data_present is None for component in components.values())
    assert all(component.launch.enabled is False for component in components.values())
    assert components["prometheus"].installed is None
    assert components["prometheus"].launch.url is None
    assert snapshot.data.signals.gpu_utilization_ratio.value is None
    assert "SENSITIVE_ALL_PROBES_FAILED" not in snapshot.model_dump_json()


@pytest.mark.parametrize(
    ("url", "hosts"),
    [
        ("http://observe.example.invalid", frozenset({"observe.example.invalid"})),
        ("https://user:secret@observe.example.invalid", frozenset({"observe.example.invalid"})),
        ("https://other.example.invalid", frozenset({"observe.example.invalid"})),
        ("https://observe.example.invalid/path?token=secret", frozenset({"observe.example.invalid"})),
        ("https://observe.example.invalid", frozenset({"*.example.invalid"})),
    ],
)
def test_observability_link_configuration_rejects_unsafe_urls(url: str, hosts: frozenset[str]) -> None:
    with pytest.raises(ValueError, match="allow-listed HTTPS|allowed host is invalid"):
        ObservabilityLinkConfig(component_urls={"grafana": url}, allowed_hosts=hosts)


@pytest.mark.parametrize("installed", [True, False])
def test_prometheus_installation_override_is_rejected(installed: bool) -> None:
    with pytest.raises(ValueError, match="self-probe"):
        PrometheusObservabilityConfig(installed_overrides={"prometheus": installed})


def test_observability_promql_is_fixed_bounded_and_identity_free() -> None:
    queries = AdminObservabilityQueryTemplates.for_window(300)
    assert set(queries) == {
        "gpu_utilization_ratio",
        "gpu_memory_utilization_ratio",
        "otel_refused_items_per_second",
        "otel_export_failures_per_second",
    }
    encoded = "\n".join(queries.values()).casefold()
    for forbidden in ("tenant", "principal", "token", "api_key", "operation_id", "model_id", "pod_uid", "uuid"):
        assert forbidden not in encoded
    assert queries["otel_refused_items_per_second"].count("or vector(0)") == 3
    assert queries["otel_export_failures_per_second"].count("or vector(0)") == 3
    with pytest.raises(ValueError, match="range"):
        AdminObservabilityQueryTemplates.for_window(59)


def test_no_gpu_family_or_region_is_hard_coded_in_adapter_source() -> None:
    source = Path(__file__).resolve().parents[1] / "src" / "fs2_serve" / "admin_adapters.py"
    encoded = source.read_text(encoding="utf-8")
    for value in (*GPU_CLASSES, "us-north1", "mk8snodegroup-"):
        assert value not in encoded
