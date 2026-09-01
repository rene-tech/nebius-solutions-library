from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from fs2_serve.admin import CachedKubernetesAdminAdapter
from fs2_serve.admin_adapters import (
    HttpPrometheusScalarReader,
    KubernetesCapacityAdminAdapter,
    KubernetesModelStateAdminAdapter,
    PrometheusModelMetricsAdminAdapter,
    PrometheusObservabilityAdminAdapter,
)
from fs2_serve.cli import _admin_read_dependencies
from fs2_serve.settings import Settings

FIXED_NOW = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
MODEL_IDS = ("hot-model", "loading-model", "cold-model", "failed-model", "unknown-model")


class FakeKubernetesReader:
    def __init__(self, values: dict[str, list[dict[str, Any]]]) -> None:
        self.values = values
        self.paths: list[str] = []

    async def list(self, path: str) -> list[dict[str, Any]]:
        self.paths.append(path)
        return self.values[path]


def _labels(model_id: str, *, style: str = "current") -> dict[str, str]:
    key = {
        "current": "fs2-serve.nebius.ai/model-id",
        "legacy": "fs2.nebius.ai/model-id",
        "application": "app.kubernetes.io/name",
    }[style]
    return {
        key: model_id,
        "app.kubernetes.io/component": "model-runtime",
        "app.kubernetes.io/instance": f"{model_id}-runtime",
    }


def _deployment(
    model_id: str,
    *,
    desired: int,
    ready: int,
    style: str = "current",
    failed: bool = False,
) -> dict[str, Any]:
    condition = (
        {"type": "Progressing", "status": "False", "reason": "ProgressDeadlineExceeded"}
        if failed
        else {"type": "Progressing", "status": "True", "reason": "NewReplicaSetAvailable"}
    )
    return {
        "metadata": {"name": f"{model_id}-runtime", "labels": _labels(model_id, style=style)},
        "spec": {"replicas": desired},
        "status": {"readyReplicas": ready, "conditions": [condition]},
    }


def _service(model_id: str, *, style: str = "current") -> dict[str, Any]:
    return {
        "metadata": {"name": model_id, "labels": _labels(model_id, style=style)},
        "spec": {"selector": {"app.kubernetes.io/instance": f"{model_id}-runtime"}},
    }


def _pod(model_id: str, *, ready: bool, failed: bool = False, style: str = "current") -> dict[str, Any]:
    status: dict[str, Any] = {
        "phase": "Running" if ready else "Pending",
        "conditions": [{"type": "Ready", "status": "True" if ready else "False"}],
        "containerStatuses": [],
    }
    if failed:
        status["containerStatuses"] = [{"state": {"waiting": {"reason": "CrashLoopBackOff"}}}]
    return {
        "metadata": {
            "name": f"{model_id}-pod",
            "uid": f"{model_id}-uid",
            "labels": _labels(model_id, style=style),
        },
        "spec": {
            "containers": [
                {
                    "resources": {
                        "requests": {"nvidia.com/gpu": "1"},
                        "limits": {"nvidia.com/gpu": "1"},
                    }
                }
            ]
        },
        "status": status,
    }


def _model_state_values() -> dict[str, list[dict[str, Any]]]:
    return {
        "/apis/apps/v1/namespaces/fs2-models/deployments": [
            _deployment("hot-model", desired=1, ready=1),
            _deployment("loading-model", desired=2, ready=0, style="application"),
            _deployment("cold-model", desired=0, ready=0, style="legacy"),
            _deployment("failed-model", desired=1, ready=0, failed=True),
        ],
        "/api/v1/namespaces/fs2-models/services": [
            _service("hot-model"),
            _service("loading-model", style="application"),
            _service("cold-model", style="legacy"),
            _service("failed-model"),
        ],
        "/api/v1/namespaces/fs2-models/pods": [
            _pod("hot-model", ready=True),
            _pod("loading-model", ready=False, style="application"),
            _pod("failed-model", ready=False, failed=True),
        ],
        "/api/v1/nodes": [
            {
                "metadata": {"labels": {"capacity.fs2.nebius/type": "preemptible"}},
                "status": {
                    "allocatable": {"nvidia.com/gpu": "8"},
                    "conditions": [{"type": "Ready", "status": "True"}],
                },
            },
            {
                "metadata": {"labels": {"capacity.fs2.nebius/type": "regular"}},
                "status": {
                    "allocatable": {"nvidia.com/gpu": "1"},
                    "conditions": [{"type": "Ready", "status": "False"}],
                },
            },
        ],
    }


@pytest.mark.asyncio
async def test_kubernetes_model_state_reconciles_hot_loading_cold_failed_and_unknown() -> None:
    reader = FakeKubernetesReader(_model_state_values())
    snapshot = await KubernetesModelStateAdminAdapter(reader, clock=lambda: FIXED_NOW).snapshot(MODEL_IDS)
    models = {model.model_id: model for model in snapshot.models}

    assert (models["hot-model"].desired_replicas, models["hot-model"].ready_replicas) == (1, 1)
    assert models["hot-model"].semantic_healthy is True
    assert (models["loading-model"].desired_replicas, models["loading-model"].ready_replicas) == (2, 0)
    assert models["loading-model"].semantic_healthy is True
    assert (models["cold-model"].desired_replicas, models["cold-model"].ready_replicas) == (0, 0)
    assert models["cold-model"].semantic_healthy is True
    assert models["failed-model"].semantic_healthy is False
    assert models["unknown-model"].semantic_healthy is None
    assert snapshot.allocatable_gpus == 9
    assert snapshot.ready_gpu_nodes == 1
    assert snapshot.preemptible_gpu_nodes == 1
    assert snapshot.active_gpu_replicas == 1
    assert reader.paths == [
        "/apis/apps/v1/namespaces/fs2-models/deployments",
        "/api/v1/namespaces/fs2-models/services",
        "/api/v1/namespaces/fs2-models/pods",
        "/api/v1/nodes",
    ]


class FakePrometheusModelReader:
    def __init__(self) -> None:
        self.scalar_queries: list[str] = []
        self.vector_queries: list[str] = []

    @staticmethod
    def _name(query: str) -> str:
        if "histogram_quantile(0.50" in query:
            return "p50"
        if "histogram_quantile(0.95" in query:
            return "p95"
        if "histogram_quantile(0.99" in query:
            return "p99"
        if 'outcome!="succeeded"' in query:
            return "error"
        if "rate(" in query:
            return "rate"
        return "terminal"

    async def scalar(self, query: str, *, at: datetime) -> float | None:
        assert at == FIXED_NOW
        self.scalar_queries.append(query)
        return {
            "rate": 2.5,
            "terminal": 10.0,
            "error": 0.1,
            "p50": 0.2,
            "p95": 0.8,
            "p99": 1.2,
        }[self._name(query)]

    async def model_vector(
        self,
        query: str,
        *,
        at: datetime,
        model_ids: tuple[str, ...],
    ) -> dict[str, float]:
        assert at == FIXED_NOW
        assert model_ids == ("hot-model", "cold-model")
        self.vector_queries.append(query)
        values = {
            "rate": (2.0, 0.5),
            "terminal": (8.0, 2.0),
            "error": (0.125, 0.0),
            "p50": (0.2, 0.3),
            "p95": (0.8, 0.9),
            "p99": (1.2, 1.4),
        }[self._name(query)]
        return dict(zip(model_ids, values, strict=True))


@pytest.mark.asyncio
async def test_prometheus_model_metrics_uses_constant_batch_query_count() -> None:
    reader = FakePrometheusModelReader()
    snapshot = await PrometheusModelMetricsAdminAdapter(reader, clock=lambda: FIXED_NOW).snapshot(
        ("hot-model", "cold-model"),
        from_at=FIXED_NOW - timedelta(hours=1),
        to_at=FIXED_NOW,
    )

    assert len(reader.scalar_queries) == len(reader.vector_queries) == 6
    assert all("hot-model" not in query and "cold-model" not in query for query in reader.vector_queries)
    assert snapshot.requests_per_second == 2.5
    assert snapshot.terminal_operations == 10
    assert snapshot.error_rate == 0.1
    models = {model.model_id: model for model in snapshot.models}
    assert models["hot-model"].requests_per_second == 2
    assert models["hot-model"].terminal_operations == 8
    assert models["cold-model"].latency_p99_seconds == 1.4


@pytest.mark.asyncio
@respx.mock
async def test_http_prometheus_model_vector_rejects_unapproved_labels() -> None:
    route = respx.post("http://prometheus.fs2-observability.svc:9090/api/v1/query").mock(
        return_value=httpx.Response(
            200,
            headers={"content-type": "application/json"},
            json={
                "status": "success",
                "data": {
                    "resultType": "vector",
                    "result": [
                        {
                            "metric": {"model": "hot-model", "pod": "sensitive-pod-name"},
                            "value": [FIXED_NOW.timestamp(), "1"],
                        }
                    ],
                },
            },
        )
    )
    reader = HttpPrometheusScalarReader("http://prometheus.fs2-observability.svc:9090")
    with pytest.raises(RuntimeError, match="unexpected label set"):
        await reader.model_vector("sum by (model) (up)", at=FIXED_NOW, model_ids=("hot-model",))
    assert route.called


def test_production_admin_composition_wires_context_and_all_live_adapters() -> None:
    from test_admin_configuration import qualified_configuration

    configuration, _ = qualified_configuration()
    settings = Settings(
        admin_capacity_enabled=True,
        admin_node_scaler_provider="nebius-managed-node-group-autoscaler",
        admin_prometheus_url="http://prometheus.fs2-observability.svc:9090",
        admin_context_project="project-test",
        admin_context_cluster="cluster-test",
        admin_context_region="eu-test1",
        admin_context_label="Test inference cluster",
    )
    kubernetes, prometheus, capacity, observability, contexts = _admin_read_dependencies(
        settings,
        initial_configuration=configuration,
    )

    assert isinstance(kubernetes, CachedKubernetesAdminAdapter)
    assert isinstance(kubernetes.delegate, KubernetesModelStateAdminAdapter)
    assert isinstance(prometheus, PrometheusModelMetricsAdminAdapter)
    assert isinstance(capacity, KubernetesCapacityAdminAdapter)
    assert capacity.config.node_scaler_provider == "nebius-managed-node-group-autoscaler"
    assert [(pool.pool_id, pool.min_nodes, pool.max_nodes) for pool in capacity.config.node_scaler_pools] == [
        (pool_id, pool.min_nodes, pool.max_nodes) for pool_id, pool in sorted(configuration.pools.items())
    ]
    assert isinstance(observability, PrometheusObservabilityAdminAdapter)
    assert contexts.options[0].model_dump() == {
        "project": "project-test",
        "cluster": "cluster-test",
        "region": "eu-test1",
        "label": "Test inference cluster",
    }


def test_admin_context_settings_are_all_or_nothing() -> None:
    with pytest.raises(ValueError, match="configured together"):
        Settings(admin_context_project="project-test")
    with pytest.raises(ValueError, match="requires a complete"):
        Settings(admin_context_label="Incomplete cluster")
    with pytest.raises(ValueError, match="requires the capacity adapter"):
        Settings(admin_node_scaler_provider="nebius-managed-node-group-autoscaler")


def test_composition_is_fail_closed_when_live_sources_are_disabled() -> None:
    kubernetes, prometheus, capacity, observability, contexts = _admin_read_dependencies(Settings())
    assert kubernetes is prometheus is capacity is observability is None
    assert contexts.options == ()
    assert Path(Settings().admin_kubernetes_token_file).name == "token"
