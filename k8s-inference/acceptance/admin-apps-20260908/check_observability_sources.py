#!/usr/bin/env python3
"""Read-only execution of the app queries through Kubernetes service proxies.

This checks real PromQL/LogQL and Pod attribution without port forwarding or
changing the live application. It is not a substitute for post-deploy API/UI
acceptance, and intentionally does not output log contents or credentials.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from datetime import UTC, datetime, timedelta

import httpx

from fs2_serve.app_observability import AppObservabilityService
from fs2_serve.apps_models import AppObservabilityTarget


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kubeconfig", required=True)
    parser.add_argument("--context", required=True)
    parser.add_argument("--namespace", default="fs2-models")
    parser.add_argument("--observability-namespace", default="fs2-observability")
    parser.add_argument("--prometheus-service", required=True)
    parser.add_argument("--loki-service", default="fs2-loki")
    parser.add_argument("--model-id", required=True)
    parser.add_argument("--deployment", required=True)
    parser.add_argument("--hours", type=float, default=1)
    args = parser.parse_args()

    async def raw(path: str) -> object:
        process = await asyncio.create_subprocess_exec(
            "kubectl", "--kubeconfig", args.kubeconfig, "--context", args.context,
            "get", "--raw", path, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        output, _ = await process.communicate()
        if process.returncode:
            raise RuntimeError("Kubernetes source query failed; no credentials or response content printed")
        return json.loads(output)

    class Reader:
        async def list(self, path):
            return (await raw(path))["items"]

    async def proxy(request):
        service = f"{args.prometheus_service}:9090" if request.url.host == "prometheus.test" else f"{args.loki_service}:3100"
        path = (
            f"/api/v1/namespaces/{args.observability_namespace}/services/{service}/proxy"
            + request.url.raw_path.decode()
        )
        return httpx.Response(200, json=await raw(path))

    service = AppObservabilityService(
        kubernetes=Reader(), prometheus_url="http://prometheus.test", loki_url="http://loki.test",
        transport=httpx.MockTransport(proxy),
    )
    target = AppObservabilityTarget(
        app_id="source-check", model_id=args.model_id, namespace=args.namespace,
        pod_labels={"fs2-serve.nebius.ai/model-deployment": args.deployment},
        execution_mode="serving", deployment_name=args.deployment,
    )
    end = datetime.now(UTC)
    start = end - timedelta(hours=args.hours)
    metrics, logs, containers = await asyncio.gather(
        service.metrics(target, start, end), service.logs(target, start, end, limit=5), service.containers(target),
    )
    print(json.dumps({
        "model_id": args.model_id, "from": start.isoformat(), "to": end.isoformat(),
        "charts": [{"id": chart.id, "state": chart.state, "reason": chart.reason,
                    "summary": chart.summary.model_dump(),
                    "points": sum(len(series.points) for series in chart.series)} for chart in metrics.charts],
        "logs": {"state": logs.state, "count": len(logs.items), "reason": logs.reason},
        "containers": {"state": containers.state, "count": containers.total, "reason": containers.reason},
    }, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
