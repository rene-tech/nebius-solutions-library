"""Resource-scoped monitoring. Queries are built here, never supplied by a browser.

The caller resolves/authorizes the resource. Prometheus and Loki of this cluster are called directly; those of
another region through that cluster's API server proxy with the api-agent kubeconfig (services/api/kube.py), so
no monitoring service of any region is exposed outside its cluster. Missing series stay missing (idle and
unavailable are different from a measured zero).
"""

import asyncio
import json
import math
import re
import time
import urllib.parse
from datetime import datetime, timezone

import httpx
from fastapi import HTTPException

import kube
from config import LOKI_URL, PROMETHEUS_URL, REGION

RANGES = {"15m": 900, "1h": 3600, "6h": 21600, "24h": 86400, "7d": 604800}


def window(period: str, end: float | None = None) -> tuple[float, float, int]:
    if period not in RANGES:
        raise HTTPException(422, "range must be 15m, 1h, 6h, 24h or 7d")
    stop = time.time() if end is None else end
    if not math.isfinite(stop) or stop > time.time() + 60 or stop < 0:
        raise HTTPException(422, "invalid end timestamp")
    return max(0, stop - RANGES[period]), stop, max(15, RANGES[period] // 240)


def target(service_url: str, region: str) -> tuple[str, dict, str | bool]:
    """(base url, headers, verify) for a monitoring service of a region: the in-cluster URL here; in another
    region the API server proxy of that cluster (`/api/v1/namespaces/<ns>/services/<name>:<port>/proxy`), with the
    bearer token and CA of its api-agent kubeconfig. 410 for a region that left the fleet (its cluster, Prometheus
    and Loki with it: the record stays, the metrics and logs are gone; attempt records and outputs remain)."""
    if region == REGION:
        return service_url, {}, True
    if region not in kube.regions():
        raise HTTPException(
            410,
            f"Region {region} is no longer part of this fleet: the metrics and logs of this operation lived "
            f"on its cluster and are gone. Its attempt records and outputs in the bucket are still there (result).",
        )
    u = urllib.parse.urlsplit(service_url)
    name, ns = u.hostname.split(".")[:2]  # <name>.<namespace>.svc[.cluster.local]
    api = kube._client(region)
    headers: dict = {}
    api.update_params_for_auth(headers, [], ["BearerToken"])  # kubernetes 36: (headers, queries, auth_settings)
    conf = api.configuration
    return (
        f"{conf.host.rstrip('/')}/api/v1/namespaces/{ns}/services/{name}:{u.port}/proxy",
        headers,
        conf.ssl_ca_cert or conf.verify_ssl,
    )


def selectors(labels: dict[str, str], regex: set[str] | None = None) -> str:
    return (
        "{" + ",".join(f"{k}{'=~' if k in (regex or set()) else '='}{json.dumps(v)}" for k, v in labels.items()) + "}"
    )


def queries(kind: str, namespace: str, name: str, step: int = 15) -> list[tuple[str, str, str, str]]:
    # RE2-escaped identity, not a user-provided regex. Kubernetes prefixes cover all revisions/attempts.
    pods = selectors(
        {"namespace": namespace, "pod": re.escape(name) + ("-predictor.*" if kind == "endpoint" else "-.*")}, {"pod"}
    )
    containers = pods[:-1] + ',container!="",container!="POD"}'
    w = f"{max(120, 2 * step)}s"  # the rate window follows the step: a coarse window still catches a short-lived pod
    q = [
        ("cpu", "CPU usage", "cores", f"sum(rate(container_cpu_usage_seconds_total{containers}[{w}]))"),
        ("memory", "Memory usage", "bytes", f"sum(container_memory_working_set_bytes{containers})"),
        ("gpu", "GPU utilization", "%", f"avg(DCGM_FI_DEV_GPU_UTIL{pods})"),
        ("gpu_memory", "GPU memory", "bytes", f"sum(DCGM_FI_DEV_FB_USED{pods}) * 1048576"),
    ]
    if kind == "endpoint":
        # the queue-proxy's http_server_request_duration_seconds histogram (otelhttp): the inference requests are the
        # POSTs; the GETs are readiness and health probes and would otherwise count as traffic. Counts per window,
        # not a per-second rate (one call every two minutes read "0.01 req/s"), and the MEAN latency from sum/count,
        # exact for any duration: the histogram's buckets end at 10 s, so a percentile of a 40 s inference read a
        # constant 10 000 ms (api 0.11.130).
        calls = selectors({"namespace": namespace, "inferenceservice": name, "http_request_method": "POST"})
        failed = calls[:-1] + ',http_response_status_code=~"5.."}'
        knative = selectors({"k8s_namespace_name": namespace, "kn_service_name": name + "-predictor"})
        seconds = max(120, 2 * step)
        per = f"{seconds // 60} min" if seconds % 60 == 0 else f"{seconds} s"
        q = [
            (
                "requests",
                f"Requests per {per}",
                "requests",
                f"round(sum(increase(http_server_request_duration_seconds_count{calls}[{w}]))) or vector(0)",
            ),
            (
                "latency",
                "Response latency · mean",
                "ms",
                f"sum(rate(http_server_request_duration_seconds_sum{calls}[{w}])) "
                f"/ sum(rate(http_server_request_duration_seconds_count{calls}[{w}])) * 1000",
            ),
            (
                "errors",
                f"Server errors per {per}",
                "requests",
                f"round(sum(increase(http_server_request_duration_seconds_count{failed}[{w}]))) or vector(0)",
            ),
            ("replicas", "Ready replicas", "replicas", f"sum(kn_revision_pods_count{knative})"),
            ("concurrency", "Concurrent requests", "requests", f"sum(kn_revision_concurrency_stable{knative})"),
        ] + q
    return q


async def _query(client: httpx.AsyncClient, url: str, params: dict, headers: dict | None = None) -> dict:
    try:
        response = await client.get(url, params=params, headers=headers)
        response.raise_for_status()
        data = response.json()
        if data.get("status") != "success":
            raise ValueError("query failed")
        return data.get("data") or {}
    except (httpx.HTTPError, ValueError, KeyError):
        # No upstream URLs, raw error bodies, or credentials in customer errors.
        raise HTTPException(503, "Monitoring is temporarily unavailable. Try again shortly.")


# An endpoint pod's start-up, phase by phase, from kube-state-metrics' timestamps (docs/API.md "Metrics"): the
# operator reads where a cold start spends its time (a node to provision, init containers, the image pull, the
# model load until the readiness probe passes) before deciding on warm pins, image caches or GPU snapshots.
STARTUP_PHASES = (
    ("created", "kube_pod_created"),
    ("scheduled", "kube_pod_status_scheduled_time"),
    ("initialized", "kube_pod_status_initialized_time"),
    ("ready", "kube_pod_status_ready_time"),
)
NEW_NODE_S = 900  # a node younger than this when the pod was scheduled was provisioned for it (a heuristic)
# A pod restored from a GPU snapshot carries this label (kube-state-metrics exports it through the fleet's label
# allowlist); no cluster of this fleet snapshots yet, so every row reads false until that lands.
GPU_SNAPSHOT_LABEL = "label_serverless2_nebius_gpu_snapshot"


def startup_queries(namespace: str, name: str, span_s: int, kind: str = "endpoint") -> dict[str, str]:
    """Instant queries over the window: an endpoint's predictor pods or a run's Job pods (`main` container)."""
    pattern = f"{re.escape(name)}-predictor-.*" if kind == "endpoint" else f"{re.escape(name)}-.*"
    pods = selectors({"namespace": namespace, "pod": pattern}, {"pod"})
    if kind == "endpoint":
        main = selectors({"namespace": namespace, "pod": pattern, "container": "queue-proxy"}, {"pod"})
        main = main.replace('container="queue-proxy"', 'container!="queue-proxy"')
    else:
        main = selectors({"namespace": namespace, "pod": pattern, "container": "main"}, {"pod"})
    rng = f"[{span_s}s]"
    q = {phase: f"last_over_time({metric}{pods}{rng})" for phase, metric in STARTUP_PHASES}
    q["started"] = f"max by(pod)(last_over_time(kube_pod_container_state_started{main}{rng}))"
    q["node"] = f"last_over_time(kube_pod_info{pods}{rng})"
    q["node_created"] = f"last_over_time(kube_pod_info{pods}{rng}) * 0 + on(node) group_left kube_node_created"
    snap = selectors({"namespace": namespace, "pod": pattern, GPU_SNAPSHOT_LABEL: "true"}, {"pod"})
    q["gpu_snapshot"] = f"last_over_time(kube_pod_labels{snap}{rng})"
    return q


def _startups(
    results: dict[str, dict], start: float, submitted: float | None = None, admitted: float | None = None
) -> list[dict]:
    """Per pod: the phase durations in seconds, pods that were created inside the window, newest first. For a run,
    `submitted` (the Job the caller created: the manager copy of a fleet-placed run) and `admitted` (its Workload's
    Admitted transition on the manager) give the queue wait (submitted → admitted) and the dispatch (admitted → the
    FIRST pod: MultiKueue's worker Job and the worker's pod creation); a later pod of the same Job is a retry
    (`attempt` > 1) and carries neither, so an earlier attempt's run time is never read as waiting."""
    stamps: dict[str, dict] = {}
    for key, data in results.items():
        for item in data.get("result", []):
            pod = item.get("metric", {}).get("pod")
            if not pod:
                continue
            row = stamps.setdefault(pod, {})
            if key == "node":
                row["node"] = item["metric"].get("node")
            elif key == "gpu_snapshot":
                row["gpu_snapshot"] = True
            else:
                try:
                    row[key] = float(item["value"][1])
                except (KeyError, ValueError, TypeError, IndexError):
                    pass
    rows = []
    ordered = sorted((t["created"], pod) for pod, t in stamps.items() if "created" in t)
    attempt_of = {pod: i + 1 for i, (_, pod) in enumerate(ordered)}
    first = ordered[0][1] if ordered else None
    for pod, t in stamps.items():
        if "created" not in t or t["created"] < start:
            continue

        def span(a, b):
            return round(t[b] - t[a], 1) if a in t and b in t else None

        node_age = t["scheduled"] - t["node_created"] if "scheduled" in t and "node_created" in t else None
        is_first = pod == first
        queue = round(admitted - submitted, 1) if is_first and admitted is not None and submitted is not None else None
        dispatch = round(t["created"] - admitted, 1) if is_first and admitted is not None else None
        rows.append(
            {
                "pod": pod,
                "node": t.get("node"),
                "attempt": attempt_of.get(pod, 1),
                "queue_wait_s": queue,
                "dispatch_s": dispatch,
                "new_node": node_age is not None and node_age < NEW_NODE_S,
                "gpu_snapshot": bool(t.get("gpu_snapshot")),
                "created_at": datetime.fromtimestamp(t["created"], tz=timezone.utc).isoformat().replace("+00:00", "Z"),
                "node_wait_s": span("created", "scheduled"),
                "init_s": span("scheduled", "initialized"),
                "pull_start_s": span("initialized", "started"),
                "load_s": span("started", "ready"),
                "total_s": span("created", "ready"),
                "ready": "ready" in t,
            }
        )
    rows.sort(key=lambda r: r["created_at"], reverse=True)
    return rows[:50]


async def metrics(
    kind: str,
    namespace: str,
    name: str,
    region: str,
    period: str,
    end: float | None = None,
    submitted: float | None = None,
    admitted: float | None = None,
) -> dict:
    start, stop, step = window(period, end)
    definitions = queries(kind, namespace, name, step)
    base, headers, verify = target(PROMETHEUS_URL, region)
    instants = startup_queries(namespace, name, int(stop - start), kind) if kind in ("endpoint", "job") else {}
    async with httpx.AsyncClient(timeout=8, verify=verify) as client:
        results = await asyncio.gather(
            *[
                _query(
                    client,
                    base + "/api/v1/query_range",
                    {"query": q, "start": start, "end": stop, "step": step},
                    headers,
                )
                for _, _, _, q in definitions
            ],
            *[_query(client, base + "/api/v1/query", {"query": q, "time": stop}, headers) for q in instants.values()],
            return_exceptions=True,
        )
    results, instant_results = results[: len(definitions)], results[len(definitions) :]
    startups = None
    if instants and not any(isinstance(r, BaseException) for r in instant_results):
        startups = _startups(dict(zip(instants, instant_results)), start, submitted, admitted)
    panels, unavailable = [], []
    for (mid, title, unit, _), result in zip(definitions, results):
        series = []
        if isinstance(result, BaseException):
            unavailable.append(mid)
        else:
            for item in result.get("result", [])[:20]:
                points = []
                for timestamp, value in item.get("values", []):
                    try:
                        number = float(value)
                        points.append([float(timestamp), number if math.isfinite(number) else None])
                    except (ValueError, TypeError):
                        continue
                series.append({"name": title, "points": points})
        panels.append({"id": mid, "title": title, "unit": unit, "series": series, "unavailable": mid in unavailable})
    if len(unavailable) == len(definitions):
        raise HTTPException(503, "Monitoring is temporarily unavailable. Try again shortly.")
    return {"region": region, "start": start, "end": stop, "step": step, "panels": panels, "startups": startups}


async def logs(
    kind: str,
    namespace: str,
    name: str,
    region: str,
    period: str,
    search: str = "",
    limit: int = 300,
    end: float | None = None,
) -> dict:
    start, stop, _ = window(period, end)
    labels = {"namespace": namespace, "inferenceservice" if kind == "endpoint" else "job": name}
    if kind == "jobset":
        # Indexed child Jobs of a JobSet share its name as their prefix.
        labels["job"] = re.escape(name) + "-.*"
    query = selectors(labels, {"job"} if kind == "jobset" else None)
    if search:
        query += " |= " + json.dumps(search)  # LogQL string literal, never executable query syntax
    base, headers, verify = target(LOKI_URL, region)
    async with httpx.AsyncClient(timeout=8, verify=verify) as client:
        data = await _query(
            client,
            base + "/loki/api/v1/query_range",
            {
                "query": query,
                "start": str(int(start * 1e9)),
                "end": str(int(stop * 1e9)),
                "limit": limit,
                "direction": "backward",
            },
            headers,
        )
    lines = []
    for stream in data.get("result", []):
        labels = stream.get("stream") or {}
        for timestamp, line in stream.get("values", []):
            lines.append(
                {
                    "timestamp": str(timestamp),
                    "line": line,
                    "pod": labels.get("pod", ""),
                    "container": labels.get("container", ""),
                }
            )
    lines.sort(key=lambda line: int(line["timestamp"]), reverse=True)
    lines = lines[:limit]
    return {"region": region, "start": start, "end": stop, "lines": lines, "truncated": len(lines) == limit}
