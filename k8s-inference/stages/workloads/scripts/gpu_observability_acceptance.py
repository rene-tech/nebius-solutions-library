"""Require one Ready DCGM exporter per created GPU node and live metrics."""

from __future__ import annotations

import json
import os
import time
import urllib.parse
import urllib.request


prometheus_url = os.environ["FS2_PROMETHEUS_URL"].rstrip("/")


def query(expression: str) -> float:
    url = prometheus_url + "/api/v1/query?" + urllib.parse.urlencode({"query": expression})
    with urllib.request.urlopen(url, timeout=20) as response:
        value = json.load(response)
    if value.get("status") != "success":
        raise RuntimeError("Prometheus query failed")
    result = value.get("data", {}).get("result", [])
    if not isinstance(result, list) or len(result) != 1:
        return 0.0
    sample = result[0].get("value")
    if not isinstance(sample, list) or len(sample) != 2:
        return 0.0
    return float(sample[1])


last = {"gpu_nodes": 0, "ready_exporters": 0, "dcgm_series": 0, "attributed_series": 0}
for _ in range(60):
    last = {
        "gpu_nodes": int(
            query(
                'count(kube_node_status_allocatable{resource="nvidia_com_gpu"} > 0)'
            )
        ),
        "ready_exporters": int(
            query(
                'count(kube_pod_status_ready{namespace="fs2-observability",'
                'pod=~"fs2-dcgm-exporter-.*",condition="true"} == 1)'
            )
        ),
        "dcgm_series": int(query("count(DCGM_FI_DEV_GPU_UTIL)")),
        "attributed_series": int(
            query(
                'count(DCGM_FI_DEV_GPU_UTIL{pod!="",namespace!="",'
                'container!="",pod_uid!=""})'
            )
        ),
    }
    if (
        last["gpu_nodes"] > 0
        and last["ready_exporters"] == last["gpu_nodes"]
        and last["dcgm_series"] > 0
        and last["attributed_series"] > 0
    ):
        print(json.dumps(last, sort_keys=True))
        break
    time.sleep(10)
else:
    raise RuntimeError("DCGM exporter readiness/metrics contract was not met")
