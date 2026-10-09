"""Capture secret-free provenance of only the NAMD operation in a client receipt."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
from uuid import UUID

from registry_probe import KUBE


def pod_record(pod):
    metadata, spec, status = pod["metadata"], pod["spec"], pod.get("status", {})
    return {"name": metadata["name"], "uid": metadata["uid"], "created_at": metadata["creationTimestamp"],
            "labels": {key: value for key, value in metadata.get("labels", {}).items() if key.startswith("fs2.nebius.ai/")},
            "phase": status.get("phase"), "node": spec.get("nodeName"), "node_selector": spec.get("nodeSelector"),
            "containers": [{"name": row["name"], "image": row["image"], "resources": row.get("resources")}
                           for row in spec["containers"]],
            "container_states": [{"name": row["name"], "image_id": row.get("imageID"),
                                  "restart_count": row.get("restartCount"),
                                  "running_since": row.get("state", {}).get("running", {}).get("startedAt"),
                                  "terminated": {key: row.get("state", {}).get("terminated", {}).get(key)
                                                 for key in ("exitCode", "reason", "startedAt", "finishedAt")}}
                                 for row in status.get("containerStatuses", [])]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--receipt", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    client = json.loads((args.receipt / "receipt.json").read_text())
    operation = str(UUID(client["operation_id"]))
    selector = f"fs2.nebius.ai/model-id=namd,fs2.nebius.ai/operation-id={operation}"
    pods = json.loads(subprocess.check_output(KUBE + ["get", "pods", "-l", selector, "-o", "json"]))["items"]
    records = []
    for pod in pods:
        record = pod_record(pod)
        records.append(record)
        running = any(row["name"] == "scientific-stage" and row["running_since"] for row in record["container_states"])
        if running:
            gpu = subprocess.run(KUBE + ["exec", record["name"], "-c", "scientific-stage", "--", "nvidia-smi",
                                         "--query-gpu=name,uuid,driver_version,compute_cap,memory.total", "--format=csv"],
                                 text=True, capture_output=True, timeout=30)
            record["gpu_query"] = {"exit_code": gpu.returncode, "stdout": gpu.stdout,
                                   "stderr": gpu.stderr[-2000:] if gpu.returncode else ""}
    result = {"model_id": "namd", "operation_id": operation, "recorded_at": datetime.now(timezone.utc).isoformat(),
              "client_receipt_directory": str(args.receipt), "pods": records,
              "scope": "read-only Pod identity and GPU query; no env, arguments, secrets or customer payloads captured"}
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps({"receipt": str(args.output), "operation_id": operation,
                      "pods": [{key: row[key] for key in ("name", "phase", "node")} for row in records]}))


if __name__ == "__main__":
    main()
