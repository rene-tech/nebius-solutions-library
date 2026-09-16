#!/usr/bin/env python3
"""Verify existing control-plane network access to both internal classifier services."""

import argparse
import json
import subprocess
from pathlib import Path

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--kubeconfig", required=True)
parser.add_argument("--context", required=True)
parser.add_argument("--output", type=Path, required=True)
args = parser.parse_args()
code = r'''
import json, time, urllib.request
rows = []
for model, suffix in [("mindguard-4b", "4b"), ("mindguard-8b", "8b")]:
    endpoint = "http://fs2-mindguard-r20260916-" + suffix + ".fs2-models.svc.cluster.local:8000/v1"
    with urllib.request.urlopen(endpoint + "/models", timeout=15) as response:
        identity = next(item for item in json.load(response)["data"] if item["id"] == model)
    payload = {"model": model, "messages": [
        {"role": "user", "content": "I am nervous about an upcoming interview."},
        {"role": "assistant", "content": "What part of the interview concerns you?"},
        {"role": "user", "content": "I would like to practice introducing myself. I feel safe and supported."}],
        "temperature": 0, "max_tokens": 15, "seed": 0}
    request = urllib.request.Request(endpoint + "/chat/completions", data=json.dumps(payload).encode(),
                                     headers={"Content-Type": "application/json"}, method="POST")
    started = time.perf_counter()
    with urllib.request.urlopen(request, timeout=120) as response:
        body = json.load(response)
        status = response.status
    choice = body["choices"][0]
    output = choice["message"]["content"].strip()
    rows.append({"model_id": model, "endpoint": endpoint, "http_status": status,
                 "served_model": identity, "latency_ms": (time.perf_counter() - started) * 1000,
                 "raw_output": output, "usage": body.get("usage"),
                 "passed": body["model"] == model and identity["max_model_len"] == 32768
                    and choice["finish_reason"] == "stop" and output == "Safety: Safe\nCategories: None"})
print(json.dumps({"path": "existing_control_plane_to_internal_model_services", "rows": rows,
                  "passed": all(row["passed"] for row in rows)}))
'''
command = ["kubectl", "--kubeconfig", args.kubeconfig, "--context", args.context,
           "-n", "fs2-system", "exec", "deployment/fs2-serve-control-plane", "-c", "control-plane",
           "--", "python3", "-c", code]
response = subprocess.run(command, check=True, capture_output=True, text=True)
result = json.loads(response.stdout)
args.output.parent.mkdir(parents=True, exist_ok=True)
args.output.write_text(json.dumps(result, indent=2) + "\n")
print(json.dumps(result, indent=2))
if not result["passed"]:
    raise SystemExit(2)
