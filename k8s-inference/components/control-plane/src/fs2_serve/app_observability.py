"""Read app charts, instance inventory and retained logs from existing sources.

Prometheus holds resource series; Loki holds logs; the existing lifecycle
ledger retains the identity of Pods that Kubernetes has already deleted. GPU
device series are attributed only during observed allocation intervals, never
by assuming every GPU on a node belongs to the app.
"""

from __future__ import annotations

import asyncio
import json
import math
import re
from collections import defaultdict
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

import httpx

from .admin import AdminAdapterUnavailableError
from .admin_adapters import KubernetesListReader
from .app_observability_models import (
    AppContainer,
    AppContainers,
    AppLogLine,
    AppLogs,
    AppMetricChart,
    AppMetricPoint,
    AppMetrics,
    AppMetricSeries,
    AppMetricSummary,
    AppPodIdentity,
)
from .apps_models import AppObservabilityTarget
from .runtime_kubernetes import GPU_ALLOCATION_OBSERVED_AT_ANNOTATION, GPU_UUIDS_ANNOTATION

MAX_PODS = 2000
MAX_POINTS = 720
MAX_RESPONSE_BYTES = 8 * 1024 * 1024


def _timestamp(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return value.astimezone(UTC) if value.tzinfo is not None else None
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return parsed.astimezone(UTC) if parsed.tzinfo is not None else None
        except ValueError:
            return None
    return None


def _finite(value: object) -> float | None:
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number >= 0 else None


def _string(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _regex(values: list[str]) -> str:
    return "|".join(re.escape(value) for value in sorted(set(values)))


def _json_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=True)


def _series_points(
    samples: list[tuple[float, float | None]],
    start: datetime,
    end: datetime,
    step: int,
) -> list[AppMetricPoint]:
    """Retain absent evaluation slots as gaps, not a line across idle periods."""
    values = {round(at * 1000): value for at, value in samples}
    count = int((end - start).total_seconds() // step) + 1
    return [
        AppMetricPoint(
            at=datetime.fromtimestamp((stamp := round((start.timestamp() + index * step) * 1000)) / 1000, UTC),
            value=values.get(stamp),
        )
        for index in range(count)
    ]


def _run_id(labels: Mapping[str, Any]) -> str | None:
    for key in ("fs2.nebius.ai/operation-id", "fs2-serve.nebius.ai/operation-id"):
        if value := _string(labels.get(key)):
            return value
    return None


def container_rows(pod: Mapping[str, Any]) -> list[AppContainer]:
    """Containers are real incarnations, including pending and init containers."""
    metadata, spec, status = (pod.get(key, {}) for key in ("metadata", "spec", "status"))
    uid = str(metadata.get("uid", ""))
    states = {
        item["name"]: item for item in [*status.get("initContainerStatuses", []), *status.get("containerStatuses", [])]
    }
    rows = []
    for item in [*spec.get("initContainers", []), *spec.get("containers", [])]:
        name = str(item["name"])
        observed = states.get(name, {})
        state = observed.get("state", {})
        phase = next(iter(state), "pending")
        detail = state.get(phase, {})
        resources = item.get("resources", {})
        requested = {**resources.get("requests", {}), **resources.get("limits", {})}
        gpu = {
            key: number
            for key, value in requested.items()
            if key == "nvidia.com/gpu" or "/mig-" in key or key in {"amd.com/gpu", "gpu.intel.com/i915"}
            if (number := _finite(value)) is not None
        }
        rows.append(
            AppContainer(
                id=f"{uid}/{name}/{observed.get('restartCount', 0)}",
                pod_uid=uid,
                pod_name=str(metadata.get("name", "")),
                namespace=str(metadata.get("namespace", "")),
                container_name=name,
                state=phase,
                reason=_string(detail.get("reason")) or _string(status.get("reason")),
                ready=observed.get("ready") is True,
                node_name=_string(spec.get("nodeName")),
                image=str(item.get("image", "")),
                started_at=_timestamp(detail.get("startedAt")),
                finished_at=_timestamp(detail.get("finishedAt")),
                restarts=int(observed.get("restartCount", 0)),
                gpu_resources=gpu,
                run_id=_run_id(metadata.get("labels", {})),
            )
        )
    return rows


def pod_identity(pod: Mapping[str, Any]) -> AppPodIdentity:
    metadata = pod.get("metadata", {})
    annotations = metadata.get("annotations", {})
    observed = _timestamp(annotations.get(GPU_ALLOCATION_OBSERVED_AT_ANNOTATION))
    windows = {}
    try:
        uuids = json.loads(annotations.get(GPU_UUIDS_ANNOTATION, "[]"))
    except (TypeError, ValueError):
        uuids = []
    # A completed GPU init container does not end a still-running model's
    # allocation. Kubelet retains the assigned device while its worker runs.
    containers = container_rows(pod)
    active = any(row.gpu_resources and row.state == "running" for row in containers)
    ends = [row.finished_at for row in containers if row.gpu_resources and row.finished_at]
    finished = max(ends) if ends and not active else None
    if observed is not None and isinstance(uuids, list):
        for uuid in uuids:
            if isinstance(uuid, str) and re.fullmatch(r"(?:GPU|MIG)-[A-Za-z0-9_.:/-]{1,123}", uuid):
                windows[uuid] = [(observed, finished)]
    return AppPodIdentity(
        namespace=str(metadata.get("namespace", "")),
        name=str(metadata.get("name", "")),
        uid=str(metadata.get("uid", "")),
        run_id=_run_id(metadata.get("labels", {})),
        gpu_windows=windows,
    )


class AppObservationHistory:
    """Reuse durable Pod/allocation facts instead of inferring deleted names."""

    def __init__(self, pool: Any) -> None:
        self.pool = pool

    async def pods(self, target: AppObservabilityTarget, start: datetime, end: datetime) -> list[AppPodIdentity]:
        async with self.pool.acquire() as connection:
            rows = await connection.fetch(
                """
                SELECT DISTINCT correlation.namespace,correlation.pod_name,correlation.pod_uid,
                       subject.operation_id::text AS operation_id
                FROM fs2_telemetry_correlations correlation
                JOIN fs2_telemetry_subjects subject USING(subject_id)
                WHERE subject.model_id=$1 AND correlation.namespace=$2
                  AND correlation.pod_name IS NOT NULL AND correlation.pod_uid IS NOT NULL
                  AND subject.accepted_at <= $4
                  AND EXISTS (SELECT 1 FROM fs2_lifecycle_signals signal
                              WHERE signal.subject_id=subject.subject_id AND signal.occurred_at >= $3)
                LIMIT 2001
                """,
                target.model_id,
                target.namespace,
                start,
                end,
            )
            allocations = await connection.fetch(
                """
                SELECT signal.pod_uid,signal.gpu_uuid,signal.interval_key,
                       min(signal.occurred_at) FILTER (WHERE signal.edge='start') AS start_at,
                       max(signal.occurred_at) FILTER (WHERE signal.edge='end') AS end_at
                FROM fs2_lifecycle_signals signal
                JOIN fs2_telemetry_subjects subject USING(subject_id)
                WHERE subject.model_id=$1 AND signal.namespace=$2
                  AND signal.clock='device_allocated' AND signal.gpu_uuid IS NOT NULL
                  AND signal.occurred_at <= $4
                GROUP BY signal.pod_uid,signal.gpu_uuid,signal.interval_key
                HAVING max(signal.occurred_at) >= $3
                LIMIT 4001
                """,
                target.model_id,
                target.namespace,
                start,
                end,
            )
        if len(rows) > MAX_PODS or len(allocations) > 4000:
            raise AdminAdapterUnavailableError("App history is too large; select a shorter time range")
        windows: dict[str, dict[str, list[tuple[datetime, datetime | None]]]] = defaultdict(dict)
        for row in allocations:
            if row["start_at"] is not None and row["end_at"] is not None:
                windows[row["pod_uid"]].setdefault(row["gpu_uuid"], []).append((row["start_at"], row["end_at"]))
        return [
            AppPodIdentity(
                row["namespace"], row["pod_name"], row["pod_uid"], row["operation_id"], windows[row["pod_uid"]]
            )
            for row in rows
        ]


class AppObservabilityService:
    def __init__(
        self,
        *,
        kubernetes: KubernetesListReader | None,
        prometheus_url: str | None,
        loki_url: str | None,
        history: AppObservationHistory | None = None,
        timeout_seconds: float = 8,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.kubernetes = kubernetes
        self.prometheus_url = prometheus_url
        self.loki_url = loki_url
        self.history = history
        self.timeout_seconds = timeout_seconds
        self.transport = transport

    async def _pods(self, target: AppObservabilityTarget) -> list[Mapping[str, Any]]:
        if self.kubernetes is None:
            raise AdminAdapterUnavailableError("Kubernetes instance observation is not configured")
        if not target.pod_labels:
            raise AdminAdapterUnavailableError("This app has no observed runtime Pod selector")
        pods = await self.kubernetes.list(f"/api/v1/namespaces/{target.namespace}/pods")
        return [
            pod
            for pod in pods
            if all(
                pod.get("metadata", {}).get("labels", {}).get(key) == value for key, value in target.pod_labels.items()
            )
        ]

    async def containers(self, target: AppObservabilityTarget) -> AppContainers:
        try:
            rows = [
                row
                for pod in await self._pods(target)
                if pod.get("status", {}).get("phase") not in {"Succeeded", "Failed"}
                for row in container_rows(pod)
            ]
            return AppContainers(app_id=target.app_id, items=rows[:1000], total=len(rows), truncated=len(rows) > 1000)
        except AdminAdapterUnavailableError as exc:
            return AppContainers(app_id=target.app_id, state="unavailable", reason=str(exc))

    async def _identities(
        self, target: AppObservabilityTarget, start: datetime, end: datetime
    ) -> tuple[list[AppPodIdentity], list[str]]:
        identities: dict[tuple[str, str], AppPodIdentity] = {}
        warnings = []
        if self.history is not None:
            try:
                for pod in await self.history.pods(target, start, end):
                    identities[(pod.namespace, pod.uid)] = pod
            except Exception:  # DB loss must not hide the still-observable live instances.
                warnings.append("Retained instance history is unavailable")
        try:
            for raw in await self._pods(target):
                pod = pod_identity(raw)
                previous = identities.get((pod.namespace, pod.uid))
                if previous:
                    # Online lifecycle intervals end with each request, not
                    # with the reusable worker's GPU reservation. A currently
                    # running GPU container remains attributable during idle
                    # time. Only that live evidence can extend closed history;
                    # a terminated Pod's stale annotation must not reopen it.
                    active_gpu = any(
                        row.gpu_resources and row.state == "running" for row in container_rows(raw)
                    )
                    pod = AppPodIdentity(
                        pod.namespace,
                        pod.name,
                        pod.uid,
                        pod.run_id or previous.run_id,
                        {**previous.gpu_windows, **pod.gpu_windows}
                        if active_gpu
                        else {**pod.gpu_windows, **previous.gpu_windows},
                    )
                identities[(pod.namespace, pod.uid)] = pod
        except AdminAdapterUnavailableError as exc:
            warnings.append(str(exc))
        return list(identities.values()), warnings

    async def _get(self, base: str, path: str, params: dict[str, str]) -> dict[str, Any]:
        try:
            async with httpx.AsyncClient(
                base_url=base.rstrip("/"),
                timeout=self.timeout_seconds,
                trust_env=False,
                transport=self.transport,
            ) as client:
                response = await client.get(path, params=params)
                response.raise_for_status()
                if len(response.content) > MAX_RESPONSE_BYTES:
                    raise AdminAdapterUnavailableError("Observability response is too large; shorten the time range")
                body = response.json()
                if not isinstance(body, dict) or body.get("status") != "success":
                    raise AdminAdapterUnavailableError("Observability query was unsuccessful")
                data = body.get("data")
                if not isinstance(data, dict):
                    raise AdminAdapterUnavailableError("Observability returned an unexpected response format")
                return data
        except (httpx.HTTPError, ValueError) as exc:
            raise AdminAdapterUnavailableError("Observability data source could not be queried") from exc

    async def _matrix(self, query: str, start: datetime, end: datetime, step: int) -> list[dict[str, Any]]:
        if not self.prometheus_url:
            raise AdminAdapterUnavailableError("Prometheus is not configured")
        data = await self._get(
            self.prometheus_url,
            "/api/v1/query_range",
            {
                "query": query,
                "start": str(start.timestamp()),
                "end": str(end.timestamp()),
                "step": str(step),
            },
        )
        if data.get("resultType") != "matrix":
            raise AdminAdapterUnavailableError("Prometheus returned an unexpected series format")
        return list(data.get("result", []))

    async def _chart(
        self,
        chart_id: str,
        title: str,
        unit: str,
        expression: str | None,
        start: datetime,
        end: datetime,
        step: int,
    ) -> AppMetricChart:
        chart = AppMetricChart(
            id=chart_id,
            title=title,
            unit=unit,
            source="prometheus",
            aggregation=f"Average and maximum of the app total in each {step}s bucket; source resolution 15s",
        )
        if expression is None:
            chart.reason = "No runtime instances are identified in the selected time range"
            return chart
        try:
            results = await asyncio.gather(
                *(
                    self._matrix(f"{function}(({expression})[{max(step, 15)}s:15s])", start, end, step)
                    for function in ("avg_over_time", "max_over_time")
                )
            )
            for series_id, label, matrix in zip(("average", "maximum"), ("Average", "Maximum"), results, strict=True):
                if len(matrix) > 1:
                    raise AdminAdapterUnavailableError("App metric aggregation returned multiple totals")
                samples = matrix[0].get("values", []) if matrix else []
                chart.series.append(
                    AppMetricSeries(
                        id=series_id,
                        label=label,
                        points=_series_points([(float(at), _finite(value)) for at, value in samples], start, end, step),
                    )
                )
            means = [point.value for point in chart.series[0].points if point.value is not None]
            peaks = [point.value for point in chart.series[1].points if point.value is not None]
            if means and peaks:
                chart.state = "available"
                chart.summary = AppMetricSummary(average=sum(means) / len(means), maximum=max(peaks))
            else:
                chart.reason = "No measurements were retained for this app in the selected range"
        except AdminAdapterUnavailableError as exc:
            chart.reason = str(exc)
        return chart

    async def _gpu_chart(
        self,
        chart_id: str,
        title: str,
        unit: str,
        metric: str,
        pods: list[AppPodIdentity],
        start: datetime,
        end: datetime,
        step: int,
        multiplier: float = 1,
    ) -> AppMetricChart:
        chart = AppMetricChart(
            id=chart_id,
            title=title,
            unit=unit,
            source="dcgm+lifecycle",
            aggregation="Mean and maximum across this app's observed allocated GPU devices at each sample",
        )
        windows: dict[str, list[tuple[datetime, datetime | None]]] = defaultdict(list)
        for pod in pods:
            for uuid, intervals in pod.gpu_windows.items():
                windows[uuid].extend(intervals)
        if not windows:
            chart.reason = "No observed GPU device allocation is available for this app"
            return chart
        try:
            query = f"max by (UUID) ({metric}{{UUID=~{_json_string(_regex(list(windows)))}}})"
            matrix = await self._matrix(query, start, end, step)
            points: dict[float, list[float]] = defaultdict(list)
            for series in matrix:
                uuid = series.get("metric", {}).get("UUID")
                for at, raw_value in series.get("values", []):
                    stamp = float(at)
                    moment = datetime.fromtimestamp(stamp, UTC)
                    value = _finite(raw_value)
                    if value is not None and any(
                        a <= moment and (b is None or moment < b) for a, b in windows.get(uuid, [])
                    ):
                        points[stamp].append(value * multiplier)
            for series_id, label in (("average", "Average per GPU"), ("maximum", "Maximum per GPU")):
                chart.series.append(
                    AppMetricSeries(
                        id=series_id,
                        label=label,
                        points=_series_points(
                            [
                                (stamp, sum(values) / len(values) if series_id == "average" else max(values))
                                for stamp, values in sorted(points.items())
                            ],
                            start,
                            end,
                            step,
                        ),
                    )
                )
            values = [value for row in points.values() for value in row]
            if values:
                chart.state = "available"
                chart.summary = AppMetricSummary(average=sum(values) / len(values), maximum=max(values))
            else:
                chart.reason = "No retained GPU samples overlap this app's observed device allocation"
        except AdminAdapterUnavailableError as exc:
            chart.reason = str(exc)
        return chart

    async def metrics(self, target: AppObservabilityTarget, start: datetime, end: datetime) -> AppMetrics:
        step = max(15, math.ceil((end - start).total_seconds() / MAX_POINTS))
        pods, warnings = await self._identities(target, start, end)
        names = _regex([pod.name for pod in pods])
        selector = f"namespace={_json_string(target.namespace)},pod=~{_json_string(names)}"
        resource_selector = selector + ',container!="",container!="POD"'
        expressions = [
            (
                "concurrent_requests",
                "Concurrent requests",
                "requests",
                f"sum(max by (model,state) (fs2_serve_operations{{model={_json_string(target.model_id)},"
                'state=~"running|activating"}))',
            ),
            (
                "cpu",
                "CPU usage",
                "cores",
                "sum(max by (namespace,pod,container) "
                f"(rate(container_cpu_usage_seconds_total{{{resource_selector}}}[2m])))"
                if names
                else None,
            ),
            (
                "memory",
                "Memory usage",
                "bytes",
                f"sum(max by (namespace,pod,container) (container_memory_working_set_bytes{{{resource_selector}}}))"
                if names
                else None,
            ),
            (
                "containers",
                "Running containers",
                "containers",
                f"sum(max by (namespace,pod,container) (kube_pod_container_status_running{{{selector}}}))"
                if names
                else None,
            ),
            (
                "ready_containers",
                "Ready containers",
                "containers",
                f"sum(max by (namespace,pod,container) (kube_pod_container_status_ready{{{selector}}}))"
                if names
                else None,
            ),
        ]
        charts = await asyncio.gather(
            *(self._chart(*item, start, end, step) for item in expressions),
            self._gpu_chart("gpu_utilization", "GPU utilization", "%", "DCGM_FI_DEV_GPU_UTIL", pods, start, end, step),
            self._gpu_chart(
                "gpu_memory", "GPU memory", "bytes", "DCGM_FI_DEV_FB_USED", pods, start, end, step, 1024 * 1024
            ),
        )
        if warnings:
            for chart in charts:
                if chart.id != "concurrent_requests" and chart.state == "available":
                    chart.state = "partial"
                    chart.reason = "; ".join(warnings)
        return AppMetrics(app_id=target.app_id, from_at=start, to_at=end, step_seconds=step, charts=list(charts))

    async def logs(
        self,
        target: AppObservabilityTarget,
        start: datetime,
        end: datetime,
        *,
        search: str = "",
        pod_name: str = "",
        container: str = "",
        limit: int = 200,
        cursor: str | None = None,
    ) -> AppLogs:
        result = AppLogs(app_id=target.app_id)
        if not self.loki_url:
            result.state, result.reason = "unavailable", "Loki log reading is not configured"
            return result
        pods, warnings = await self._identities(target, start, end)
        allowed = {pod.name: pod for pod in pods}
        if pod_name:
            allowed = {pod_name: allowed[pod_name]} if pod_name in allowed else {}
        if not allowed:
            result.reason = "No app instances are identified in the selected time range"
            result.state = "unavailable" if warnings else "available"
            return result
        query = (
            f"{{k8s_namespace_name={_json_string(target.namespace)},"
            f"k8s_pod_name=~{_json_string(_regex(list(allowed)))}}}"
        )
        if container:
            query = query[:-1] + f",k8s_container_name={_json_string(container)}}}"
        if search:
            query += f" |= {_json_string(search)}"
        end_ns = int(end.timestamp() * 1_000_000_000)
        offset = 0
        if cursor is not None:
            match = re.fullmatch(r"([0-9]{1,20}):([0-9]{1,4})", cursor)
            if match is None or int(match[2]) > 4500:
                raise ValueError("invalid log cursor")
            end_ns = min(end_ns, int(match[1]))
            offset = int(match[2])
        try:
            data = await self._get(
                self.loki_url,
                "/loki/api/v1/query_range",
                {
                    "query": query,
                    "start": str(int(start.timestamp() * 1_000_000_000)),
                    "end": str(end_ns),
                    # Read ahead so equal-timestamp lines have stable ordering.
                    # Loki has no opaque cursor: retain a boundary offset rather
                    # than subtracting 1ns and silently dropping tied log lines.
                    "limit": "5000",
                    "direction": "backward",
                },
            )
            if data.get("resultType") != "streams":
                raise AdminAdapterUnavailableError("Loki returned an unexpected log format")
            rows: list[tuple[int, AppLogLine]] = []
            for stream in data.get("result", []):
                labels = stream.get("stream", {})
                name = labels.get("k8s_pod_name", "")
                if name not in allowed or labels.get("k8s_namespace_name") != target.namespace:
                    continue
                for at, message in stream.get("values", []):
                    level = None
                    try:
                        body = json.loads(message)
                        if isinstance(body, dict):
                            level = _string(body.get("level")) or _string(body.get("severity_text"))
                    except (TypeError, ValueError):
                        pass
                    stamp = int(at)
                    rows.append(
                        (
                            stamp,
                            AppLogLine(
                                at=datetime.fromtimestamp(stamp / 1_000_000_000, UTC),
                                message=str(message),
                                namespace=target.namespace,
                                pod=name,
                                container=str(labels.get("k8s_container_name", "")),
                                level=level,
                                run_id=allowed[name].run_id,
                            ),
                        )
                    )
            rows.sort(key=lambda item: (item[0], item[1].pod, item[1].container, item[1].message), reverse=True)
            selected = rows[offset : offset + limit]
            # If all the read-ahead limit is one timestamp, pagination cannot
            # identify the unseen tied lines. Explain the bound, never skip it.
            boundary_incomplete = len(rows) == 5000 and bool(selected) and selected[-1][0] == rows[-1][0]
            result.items = [line for _, line in selected]
            result.truncated = len(rows) > offset + limit or len(rows) == 5000
            if result.truncated and selected and not boundary_incomplete:
                boundary = selected[-1][0]
                consumed = sum(stamp == boundary for stamp, _ in rows[: offset + limit])
                result.next_cursor = f"{boundary}:{consumed}"
            if boundary_incomplete:
                warnings.append("More than 5000 log lines share the page boundary; filter by container or search")
            if warnings:
                result.state, result.reason = "partial", "; ".join(warnings)
        except AdminAdapterUnavailableError as exc:
            result.state, result.reason = "unavailable", str(exc)
        return result
