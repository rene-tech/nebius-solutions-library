#!/usr/bin/env python3
"""Record task-owned preview provenance and startup clocks without reading any secrets."""

from __future__ import annotations

import argparse
import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--kubeconfig", required=True)
parser.add_argument("--context", required=True)
parser.add_argument("--model", choices=["mindguard-4b", "mindguard-8b"], required=True)
parser.add_argument("--cache-state", choices=["cold-image-cold-weights", "cold-image-warm-weights",
                                             "warm-image-cold-weights", "warm-image-warm-weights"],
                    required=True)
parser.add_argument("--output", type=Path, required=True)
args = parser.parse_args()
base = ["kubectl", "--kubeconfig", args.kubeconfig, "--context", args.context, "-n", "fs2-models"]
name = "fs2-mindguard-r20260916-" + args.model.removeprefix("mindguard-")


def call(*command: str) -> str:
    return subprocess.run([*base, *command], check=True, capture_output=True, text=True).stdout


pods = json.loads(call("get", "pods", "-l", "app.kubernetes.io/name=" + name, "-o", "json"))["items"]
ready = [pod for pod in pods if any(c["type"] == "Ready" and c["status"] == "True"
                                  for c in pod["status"].get("conditions", []))]
if len(ready) != 1:
    raise SystemExit("expected exactly one ready task-owned preview pod")
pod = ready[0]
pod_name = pod["metadata"]["name"]
events = json.loads(call("get", "events", "--field-selector", "involvedObject.name=" + pod_name, "-o", "json"))
gpu = call("exec", pod_name, "-c", "vllm", "--", "nvidia-smi",
           "--query-gpu=name,uuid,memory.total,memory.used,utilization.gpu,driver_version", "--format=csv,noheader")
versions = call("exec", pod_name, "-c", "vllm", "--", "python3", "-c",
                "import json,vllm,torch,transformers;print(json.dumps({'vllm':vllm.__version__,"
                "'torch':torch.__version__,'transformers':transformers.__version__,'cuda':torch.version.cuda}))")
memory_log = [line for line in call("logs", pod_name, "-c", "vllm", "--tail=500").splitlines()
              if any(marker in line for marker in ["Available KV cache memory", "GPU KV cache size", "Actual usage is"])]
clock = lambda value: datetime.fromisoformat(value.replace("Z", "+00:00"))
ready_at = next(c["lastTransitionTime"] for c in pod["status"]["conditions"] if c["type"] == "Ready")
hydrate = pod["status"]["initContainerStatuses"][0]["state"]["terminated"]
runtime_start = pod["status"]["containerStatuses"][0]["state"]["running"]["startedAt"]
record = {
    "observed_at": datetime.now(UTC).isoformat(), "context": args.context, "namespace": "fs2-models",
    "region": "eu-north1", "capacity_type": "regular", "preemptible": False,
    "capacity_reason": "Existing free L40S; preemptible H100 nodes were NotReady at scheduling.",
    "model_id": args.model, "cache_state": args.cache_state, "pod_name": pod_name,
    "pod_uid": pod["metadata"]["uid"], "node": pod["spec"]["nodeName"],
    "model_revision": pod["metadata"]["annotations"]["fs2.nebius/model-revision"],
    "runtime_args": pod["spec"]["containers"][0]["args"],
    "requested_image": pod["spec"]["containers"][0]["image"],
    "image_id": pod["status"]["containerStatuses"][0]["imageID"],
    "gpu_csv_fields": "name,uuid,memory.total,memory.used,utilization.gpu,driver_version",
    "gpu_csv": gpu.strip(), "software": json.loads(versions), "memory_and_kv_log": memory_log,
    "clocks": {"pod_created_at": pod["metadata"]["creationTimestamp"], "ready_at": ready_at,
               "hydrate_started_at": hydrate["startedAt"], "hydrate_finished_at": hydrate["finishedAt"],
               "runtime_started_at": runtime_start,
               "pod_to_ready_seconds": (clock(ready_at) - clock(pod["metadata"]["creationTimestamp"])).total_seconds(),
               "hydrate_seconds": (clock(hydrate["finishedAt"]) - clock(hydrate["startedAt"])).total_seconds(),
               "runtime_to_ready_seconds": (clock(ready_at) - clock(runtime_start)).total_seconds()},
    "events": [{"reason": event["reason"], "message": event["message"],
                "first_at": event.get("firstTimestamp"), "last_at": event.get("lastTimestamp")}
               for event in events["items"]],
}
args.output.parent.mkdir(parents=True, exist_ok=True)
args.output.write_text(json.dumps(record, indent=2) + "\n")
print(json.dumps({"model_id": args.model, "pod_name": pod_name, "software": record["software"],
                  "clocks": record["clocks"]}, indent=2))
