from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import httpx
import pytest

from fs2_serve.app_observability import AppObservabilityService, container_rows, pod_identity
from fs2_serve.app_observability_models import AppPodIdentity
from fs2_serve.apps_models import AppObservabilityTarget
from fs2_serve.runtime_kubernetes import GPU_ALLOCATION_OBSERVED_AT_ANNOTATION, GPU_UUIDS_ANNOTATION

START = datetime(2026, 9, 8, 10, tzinfo=UTC)
END = START + timedelta(minutes=10)


def target(route="app-a"):
    return AppObservabilityTarget(
        app_id=route,
        model_id=route,
        namespace="models",
        pod_labels={"fs2-serve.nebius.ai/model-deployment": route},
        execution_mode="serving",
    )


def pod(name="a", route="app-a", phase="Running", gpu_uuid="GPU-111"):
    return {
        "metadata": {
            "name": name,
            "namespace": "models",
            "uid": f"uid-{name}",
            "labels": {"fs2-serve.nebius.ai/model-deployment": route},
            "annotations": {
                GPU_UUIDS_ANNOTATION: json.dumps([gpu_uuid]),
                GPU_ALLOCATION_OBSERVED_AT_ANNOTATION: START.isoformat(),
            },
        },
        "spec": {
            "nodeName": "shared-node",
            "initContainers": [{"name": "cache", "image": "cache:1"}],
            "containers": [
                {
                    "name": "model",
                    "image": "model:1",
                    "resources": {
                        "requests": {"nvidia.com/gpu": "1"},
                        "limits": {"nvidia.com/gpu": "1"},
                    },
                }
            ],
        },
        "status": {
            "phase": phase,
            "initContainerStatuses": [
                {
                    "name": "cache",
                    "ready": False,
                    "state": {
                        "terminated": {"reason": "Completed", "finishedAt": START.isoformat()},
                    },
                }
            ],
            "containerStatuses": [
                {
                    "name": "model",
                    "ready": True,
                    "restartCount": 2,
                    "state": {"running": {"startedAt": START.isoformat()}},
                }
            ],
        },
    }


def service(pods=(), handler=None, history=None):
    reader = AsyncMock()
    reader.list.return_value = list(pods)
    return AppObservabilityService(
        kubernetes=reader,
        prometheus_url="http://prometheus.test",
        loki_url="http://loki.test",
        history=history,
        transport=httpx.MockTransport(handler) if handler else None,
    )


def test_real_container_incarnations_and_init_states():
    rows = container_rows(pod())
    assert len(rows) == 2
    assert rows[0].state == "terminated"
    assert rows[1].id == "uid-a/model/2"
    assert rows[1].ready and rows[1].node_name == "shared-node"
    assert rows[1].gpu_resources == {"nvidia.com/gpu": 1}
    assert rows[1].run_id is None  # batch IDs must never masquerade as run IDs.


@pytest.mark.asyncio
async def test_two_apps_with_same_model_and_node_have_separate_current_containers():
    observed = service([pod(), pod("b", "app-b"), pod("terminal", phase="Succeeded")])
    a = await observed.containers(target())
    b = await observed.containers(target("app-b"))
    assert {row.pod_name for row in a.items} == {"a"}
    assert {row.pod_name for row in b.items} == {"b"}
    assert a.total == b.total == 2


@pytest.mark.asyncio
async def test_empty_selector_never_lists_other_apps():
    observed = service([pod()])
    result = await observed.containers(target().model_copy(update={"pod_labels": {}}))
    assert result.state == "unavailable" and result.items == []
    observed.kubernetes.list.assert_not_awaited()


def test_observed_gpu_interval_stops_at_gpu_container_end():
    value = pod()
    value["status"]["containerStatuses"][0]["state"] = {"terminated": {"finishedAt": END.isoformat()}}
    identity = pod_identity(value)
    assert identity.gpu_windows == {"GPU-111": [(START, END)]}


@pytest.mark.asyncio
async def test_gpu_attribution_requires_uuid_and_allocation_time_not_exporter_pod():
    stop = START + timedelta(minutes=2)
    identity = AppPodIdentity("models", "deleted", "uid-deleted", gpu_windows={"GPU-owned": [(START, stop)]})
    queries = []

    def handler(request):
        queries.append(request.url.params["query"])
        return httpx.Response(
            200,
            json={
                "status": "success",
                "data": {
                    "resultType": "matrix",
                    "result": [
                        {
                            "metric": {"UUID": "GPU-owned", "pod": "dcgm-exporter"},
                            "values": [
                                [(START - timedelta(seconds=15)).timestamp(), "99"],
                                [START.timestamp(), "20"],
                                [(stop - timedelta(seconds=15)).timestamp(), "40"],
                                [stop.timestamp(), "100"],
                            ],
                        },
                        {
                            "metric": {"UUID": "GPU-other", "pod": "dcgm-exporter"},
                            "values": [[START.timestamp(), "100"]],
                        },
                    ],
                },
            },
        )

    chart = await service(handler=handler)._gpu_chart(
        "gpu_utilization",
        "GPU utilization",
        "%",
        "DCGM_FI_DEV_GPU_UTIL",
        [identity],
        START,
        END,
        15,
    )
    assert chart.state == "available"
    assert chart.summary.average == 30 and chart.summary.maximum == 40
    assert [point.value for point in chart.series[0].points if point.value is not None] == [20, 40]
    assert chart.series[0].points[-1].value is None
    assert 'UUID=~"GPU\\\\-owned"' in queries[0]


@pytest.mark.asyncio
async def test_no_metrics_is_unavailable_not_zero():
    def handler(_):
        return httpx.Response(200, json={"status": "success", "data": {"resultType": "matrix", "result": []}})

    result = await service([pod()], handler).metrics(target(), START, END)
    assert len(result.charts) == 7
    assert all(chart.state == "unavailable" and chart.summary.average is None for chart in result.charts)


@pytest.mark.asyncio
async def test_cpu_and_memory_deduplicate_scrapers_and_exclude_pod_total():
    queries = []

    def handler(request):
        queries.append(request.url.params["query"])
        return httpx.Response(200, json={"status": "success", "data": {"resultType": "matrix", "result": []}})

    await service([pod()], handler).metrics(target(), START, END)
    cpu = next(query for query in queries if "container_cpu_usage" in query)
    assert 'container!=""' in cpu and 'container!="POD"' in cpu
    assert "max by (namespace,pod,container)" in cpu and "avg_over_time" in cpu
    assert 'pod=~"a"' in cpu


@pytest.mark.asyncio
async def test_deleted_pod_logs_are_retained_and_queries_are_exact_and_escaped():
    history = AsyncMock()
    history.pods.return_value = [AppPodIdentity("models", "deleted-app", "uid-old", "operation-1")]
    queries = []

    def handler(request):
        queries.append(request.url.params["query"])
        return httpx.Response(
            200,
            json={
                "status": "success",
                "data": {
                    "resultType": "streams",
                    "result": [
                        {
                            "stream": {
                                "k8s_namespace_name": "models",
                                "k8s_pod_name": "deleted-app",
                                "k8s_container_name": "model",
                            },
                            "values": [[str(int(START.timestamp() * 1e9)), '{"level":"info","message":"hello"}']],
                        },
                        {
                            "stream": {"k8s_namespace_name": "other-tenant", "k8s_pod_name": "deleted-app"},
                            "values": [[str(int(START.timestamp() * 1e9)), "must not appear"]],
                        },
                    ],
                },
            },
        )

    result = await service(handler=handler, history=history).logs(target(), START, END, search='quote"')
    assert len(result.items) == 1 and result.items[0].run_id == "operation-1"
    assert result.items[0].level == "info"
    assert 'k8s_namespace_name="models"' in queries[0]
    assert queries[0].endswith('|= "quote\\""')


@pytest.mark.asyncio
async def test_log_cursor_preserves_tied_timestamps_across_pages():
    stamp = int(START.timestamp() * 1e9)
    messages = [(stamp + 10, "new"), (stamp, "c"), (stamp, "b"), (stamp, "a"), (stamp - 10, "old")]

    def handler(request):
        end = int(request.url.params["end"])
        return httpx.Response(
            200,
            json={
                "status": "success",
                "data": {
                    "resultType": "streams",
                    "result": [
                        {
                            "stream": {
                                "k8s_namespace_name": "models",
                                "k8s_pod_name": "a",
                                "k8s_container_name": "model",
                            },
                            "values": [[str(at), message] for at, message in messages if at <= end],
                        }
                    ],
                },
            },
        )

    observed = service([pod()], handler)
    result = await observed.logs(target(), START - timedelta(seconds=1), END, limit=2)
    found = list(result.items)
    while result.next_cursor:
        result = await observed.logs(target(), START - timedelta(seconds=1), END, limit=2, cursor=result.next_cursor)
        found.extend(result.items)
    assert [row.message for row in found] == ["new", "c", "b", "a", "old"]


@pytest.mark.asyncio
async def test_history_failure_keeps_live_data_but_reports_partial():
    history = AsyncMock()
    history.pods.side_effect = RuntimeError("database unavailable")

    def handler(_):
        return httpx.Response(200, json={"status": "success", "data": {"resultType": "streams", "result": []}})

    result = await service([pod()], handler, history).logs(target(), START, END)
    assert result.state == "partial" and "history" in result.reason


@pytest.mark.asyncio
async def test_malformed_source_response_is_explicitly_unavailable():
    result = await service([pod()], lambda _: httpx.Response(200, json=[])).logs(target(), START, END)
    assert result.state == "unavailable"


@pytest.mark.asyncio
async def test_closed_historical_allocations_are_not_reopened_by_stale_pod_annotation():
    history = AsyncMock()
    history.pods.return_value = [AppPodIdentity("models", "a", "uid-a", gpu_windows={"GPU-111": [(START, END)]})]
    identities, _ = await service([pod()], history=history)._identities(target(), START, END)
    assert identities[0].gpu_windows["GPU-111"] == [(START, END)]
