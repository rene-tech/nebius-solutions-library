#!/usr/bin/env python3
"""Read-only customer-trial sampler; credentials and raw responses stay private.

Only creates/deletes its own admin login session. Does not invoke models or alter
resources. Missing Prometheus series remain empty, never synthetic zeroes.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import signal
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx


QUERIES = {
    "gpu_utilization": "DCGM_FI_DEV_GPU_UTIL",
    "gpu_memory_used_mib": "DCGM_FI_DEV_FB_USED",
    "gpu_power_watts": "DCGM_FI_DEV_POWER_USAGE",
    "node_cpu_busy_percent": '100 * (1 - avg by (instance) (rate(node_cpu_seconds_total{mode="idle"}[2m])))',
    "node_memory_used_percent": "100 * (1 - node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes)",
    "node_root_disk_used_percent": '100 * (1 - node_filesystem_avail_bytes{mountpoint="/",fstype!~"tmpfs|overlay"} / node_filesystem_size_bytes{mountpoint="/",fstype!~"tmpfs|overlay"})',
    "pod_cpu_cores": 'sum by (namespace,pod) (rate(container_cpu_usage_seconds_total{namespace=~"fs2-system|fs2-models",container!="",container!="POD"}[2m]))',
    "pod_memory_bytes": 'sum by (namespace,pod) (container_memory_working_set_bytes{namespace=~"fs2-system|fs2-models",container!="",container!="POD"})',
    # Durable database projections repeat on each CP replica: max deduplicates.
    "operations": "max by (model,state) (fs2_serve_operations)",
    "oldest_queue_seconds": "max by (model) (fs2_serve_oldest_queued_operation_age_seconds)",
    "terminal_operations": "max by (model,protocol,outcome) (fs2_serve_requests_total)",
    "lifecycle_gpu_seconds": "max by (tenant,model,phase,quality) (fs2_serve_lifecycle_gpu_seconds_total)",
    "lifecycle_clock_gpu_seconds": "max by (tenant,model,clock,quality,reconciled) (fs2_serve_lifecycle_clock_gpu_seconds_total)",
    "lifecycle_unclassified_gpu_seconds": "max by (tenant,model,quality,reconciled) (fs2_serve_lifecycle_unclassified_gpu_seconds_total)",
    "scrape_health": "up",
    "node_root_disk_available_bytes": 'node_filesystem_avail_bytes{mountpoint="/",fstype!~"tmpfs|overlay"}',
    "node_root_disk_size_bytes": 'node_filesystem_size_bytes{mountpoint="/",fstype!~"tmpfs|overlay"}',
}


def utc() -> str:
    return datetime.now(UTC).isoformat()


def pod_projection(item: dict) -> dict:
    metadata, spec, status = item["metadata"], item["spec"], item.get("status", {})
    return {
        "name": metadata["name"],
        "uid": metadata["uid"],
        "namespace": metadata["namespace"],
        "created_at": metadata["creationTimestamp"],
        "labels": metadata.get("labels", {}),
        "node": spec.get("nodeName"),
        "phase": status.get("phase"),
        "conditions": status.get("conditions", []),
        "container_statuses": status.get("containerStatuses", []),
        "init_container_statuses": status.get("initContainerStatuses", []),
        "resources": [
            {"name": c["name"], **c.get("resources", {})} for c in spec["containers"]
        ],
        "init_resources": [
            {"name": c["name"], **c.get("resources", {})}
            for c in spec.get("initContainers", [])
        ],
        "deletion_timestamp": metadata.get("deletionTimestamp"),
    }


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--credential-bundle", type=Path, required=True)
    parser.add_argument("--kubeconfig", required=True)
    parser.add_argument("--context", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--interval", type=float, default=25)
    parser.add_argument(
        "--samples", type=int, default=0, help="0 runs until SIGINT/SIGTERM"
    )
    args = parser.parse_args()
    args.output.mkdir(mode=0o700, parents=True, exist_ok=True)
    access = json.loads(args.credential_bundle.read_bytes())
    origin = access["endpoints"]["inference_base_url"].removesuffix("/v1")
    grafana = access["endpoints"]["grafana_url"].rstrip("/")
    creds = access["credentials"]["grafana"]
    grafana_auth = httpx.BasicAuth(creds["username"], creds["password"])
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    kubectl = [
        "kubectl",
        "--kubeconfig",
        args.kubeconfig,
        "--context",
        args.context,
        "--request-timeout=15s",
    ]

    async def kube(*command: str) -> dict:
        started = time.monotonic()
        proc = await asyncio.create_subprocess_exec(
            *kubectl,
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), 20)
        except TimeoutError:
            proc.kill()
            await proc.wait()
            return {
                "error": "kubectl_timeout",
                "elapsed_seconds": time.monotonic() - started,
            }
        result = {
            "elapsed_seconds": time.monotonic() - started,
            "returncode": proc.returncode,
        }
        if proc.returncode:
            result["error"] = stderr.decode()[:1000]
        else:
            result["data"] = json.loads(stdout)
        return result

    async with httpx.AsyncClient(
        base_url=origin,
        headers={"origin": origin},
        timeout=20,
        limits=httpx.Limits(max_connections=12),
    ) as client:
        login = await client.post(
            "/admin/api/v1/session",
            headers={
                "authorization": "Bearer "
                + access["credentials"]["admin_bootstrap_token"]
            },
        )
        login.raise_for_status()

        async def get(url: str, **kwargs) -> dict:
            started = time.monotonic()
            try:
                response = await client.get(url, **kwargs)
                result = {
                    "status": response.status_code,
                    "elapsed_seconds": time.monotonic() - started,
                }
                try:
                    result["data"] = response.json()
                except ValueError:
                    result["non_json_body_bytes"] = len(response.content)
                return result
            except httpx.HTTPError as exc:
                return {
                    "error": type(exc).__name__,
                    "elapsed_seconds": time.monotonic() - started,
                }

        try:
            sources = await get(grafana + "/api/datasources", auth=grafana_auth)
            (args.output / "datasources.json").write_text(json.dumps(sources, indent=2))
            prometheus = next(
                row for row in sources.get("data", []) if row["type"] == "prometheus"
            )
            prom_url = (
                grafana
                + "/api/datasources/proxy/uid/"
                + prometheus["uid"]
                + "/api/v1/query"
            )
            count = 0
            with (args.output / "samples.jsonl").open("x") as output:
                while not stop.is_set():
                    sample_started = time.monotonic()
                    stamp = utc()
                    calls = {
                        "pods": kube("get", "pods", "-A", "-o", "json"),
                        "nodes": kube("get", "nodes", "-o", "json"),
                        "workloads": kube(
                            "get",
                            "workloads.kueue.x-k8s.io",
                            "-A",
                            "-o",
                            "json",
                        ),
                        "qwen_model_deployment": kube(
                            "get",
                            "modeldeployments.inference.fs2.nebius.ai",
                            "qwen3-8b",
                            "-n",
                            "fs2-models",
                            "-o",
                            "json",
                        ),
                        "capacity": get("/admin/api/v1/capacity"),
                        "operations": get(
                            "/admin/api/v1/operations", params={"limit": 200}
                        ),
                        "telemetry": get(
                            "/admin/api/v1/telemetry/workloads", params={"limit": 200}
                        ),
                        "overview": get("/admin/api/v1/overview"),
                    }
                    calls.update(
                        {
                            "prom_" + name: get(
                                prom_url, params={"query": query}, auth=grafana_auth
                            )
                            for name, query in QUERIES.items()
                        }
                    )
                    values = await asyncio.gather(*calls.values())
                    sample = {
                        "index": count,
                        "started_at": stamp,
                        **dict(zip(calls, values, strict=True)),
                    }
                    if "data" in sample["pods"]:
                        sample["pods"]["data"] = [
                            pod_projection(item)
                            for item in sample["pods"]["data"]["items"]
                        ]
                    if "data" in sample["nodes"]:
                        sample["nodes"]["data"] = [
                            {
                                "name": x["metadata"]["name"],
                                "labels": x["metadata"].get("labels", {}),
                                "status": x["status"],
                            }
                            for x in sample["nodes"]["data"]["items"]
                        ]
                    sample["completed_at"] = utc()
                    sample["elapsed_seconds"] = time.monotonic() - sample_started
                    output.write(json.dumps(sample, separators=(",", ":")) + "\n")
                    output.flush()
                    pods = sample["pods"].get("data", [])
                    print(
                        json.dumps(
                            {
                                "sample": count,
                                "utc": stamp,
                                "elapsed_seconds": round(sample["elapsed_seconds"], 3),
                                "pod_phases": {
                                    phase: sum(p["phase"] == phase for p in pods)
                                    for phase in (
                                        "Running",
                                        "Pending",
                                        "Failed",
                                        "Succeeded",
                                    )
                                },
                                "admin_status": {
                                    name: sample[name].get(
                                        "status", sample[name].get("error")
                                    )
                                    for name in (
                                        "capacity",
                                        "operations",
                                        "telemetry",
                                        "overview",
                                    )
                                },
                                "missing_metrics": [
                                    name
                                    for name in QUERIES
                                    if not sample["prom_" + name]
                                    .get("data", {})
                                    .get("data", {})
                                    .get("result")
                                ],
                            }
                        ),
                        flush=True,
                    )
                    count += 1
                    if args.samples and count >= args.samples:
                        break
                    try:
                        await asyncio.wait_for(
                            stop.wait(),
                            max(
                                0.1, args.interval - (time.monotonic() - sample_started)
                            ),
                        )
                    except TimeoutError:
                        pass
            (args.output / "sampler-completed.json").write_text(
                json.dumps(
                    {
                        "completed_at": utc(),
                        "samples": count,
                        "reason": "signal" if stop.is_set() else "sample_limit",
                    }
                )
            )
        finally:
            await client.delete("/admin/api/v1/session")


if __name__ == "__main__":
    asyncio.run(main())
