"""Collect non-secret task pod, image-cache, GPU sample and cold-start provenance."""

import argparse
import csv
import hashlib
import json
import statistics
import subprocess
from datetime import datetime
from pathlib import Path


def pod_row(pod):
    conditions = {c["type"]: c for c in pod["status"].get("conditions", [])}
    status = pod["status"].get("containerStatuses", [{}])[0]
    ready = conditions.get("Ready", {})
    created = pod["metadata"]["creationTimestamp"]
    ready_at = (
        ready.get("lastTransitionTime") if ready.get("status") == "True" else None
    )
    return {
        "name": pod["metadata"]["name"],
        "uid": pod["metadata"]["uid"],
        "model": pod["metadata"]["labels"].get("fs2.voice/model"),
        "node": pod["spec"].get("nodeName"),
        "created_at": created,
        "started_at": status.get("state", {}).get("running", {}).get("startedAt"),
        "ready_at": ready_at,
        "creation_to_ready_seconds": (
            datetime.fromisoformat(ready_at) - datetime.fromisoformat(created)
        ).total_seconds()
        if ready_at
        else None,
        "image": pod["spec"]["containers"][0]["image"],
        "image_id": status.get("imageID"),
        "image_pull_policy": pod["spec"]["containers"][0]["imagePullPolicy"],
        "restarts": status.get("restartCount"),
        "gpu_request": pod["spec"]["containers"][0]["resources"]["requests"].get(
            "nvidia.com/gpu"
        ),
    }


def main(args):
    root = Path(args.output)
    root.mkdir(parents=True, exist_ok=True)
    kubectl = [
        "kubectl",
        "--kubeconfig",
        args.kubeconfig,
        "--context",
        args.context,
        "-n",
        "fs2-models",
    ]
    pods = json.loads(
        subprocess.check_output(
            kubectl
            + [
                "get",
                "pods",
                "-l",
                "workload.fs2.nebius/owner=voice-agent-20260916",
                "-o",
                "json",
            ]
        )
    )["items"]
    results = {
        "context": args.context,
        "namespace": "fs2-models",
        "project": "project-e00rene",
        "region": "eu-north1",
        "cluster": "mk8scluster-e00j5z9te7x5dd9g6a",
        "capacity": "existing regular L40S headroom; preemptible H100 NotReady",
        "pods": [pod_row(pod) for pod in pods],
        "initial_pods": [
            pod_row(p) for p in json.loads(Path(args.initial_pods).read_text())["items"]
        ],
        "gpu_samples": {},
        "snapshot": {
            "enabled": False,
            "criu_installed": False,
            "cuda_checkpoint_installed": False,
            "restored_public_cohort": False,
        },
        "framework": {
            "torch": "2.8.0+cu128",
            "cuda": "12.8",
            "compute_capability": "8.9",
            "driver": "580.173.02",
        },
    }
    for short in ("magpie", "parakeet", "sortformer"):
        path = Path(args.samples) / f"fs2-voice-{short}-gpu-r20260916.csv"
        data = path.read_bytes()
        (root / path.name).write_bytes(data)
        rows = list(csv.reader(data.decode().splitlines()))
        memory = [float(row[3].strip().split()[0]) for row in rows]
        utilization = [float(row[4].strip().split()[0]) for row in rows]
        results["gpu_samples"][short] = {
            "file": path.name,
            "sha256": hashlib.sha256(data).hexdigest(),
            "samples": len(rows),
            "start": rows[0][0],
            "end": rows[-1][0],
            "memory_peak_mib": max(memory),
            "utilization_peak_percent": max(utilization),
            "utilization_mean_percent": statistics.mean(utilization),
            "note": "500ms nvidia-smi samples spanning sequential warm/native tests and idle; not a sustained-throughput claim",
        }
    events = json.loads(
        subprocess.check_output(kubectl + ["get", "events", "-o", "json"])
    )["items"]
    results["image_events"] = [
        {
            "pod": e["involvedObject"]["name"],
            "reason": e["reason"],
            "message": e["message"],
            "first_at": e.get("firstTimestamp"),
            "last_at": e.get("lastTimestamp"),
        }
        for e in events
        if e["involvedObject"]["name"].startswith("fs2-voice-")
        and e["reason"] in {"Pulled", "Pulling", "Scheduled"}
    ]
    (root / "runtime-provenance.json").write_text(json.dumps(results, indent=2) + "\n")
    print(
        json.dumps(
            {
                "pods": len(pods),
                "samples": {k: v["samples"] for k, v in results["gpu_samples"].items()},
            }
        )
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for key in ("kubeconfig", "context", "initial-pods", "samples", "output"):
        parser.add_argument("--" + key, required=True)
    main(parser.parse_args())
