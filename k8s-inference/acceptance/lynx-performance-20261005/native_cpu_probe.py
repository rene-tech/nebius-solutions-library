"""Operator-only CPU-envelope experiment, not a newly exposed public shape.

Runs the retained native CLI harness with an explicit CPU request/limit. Public
GROMACS currently has eight CPUs per rank; this does not bypass that contract.
"""

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import sys
import time

from recipes import ROOT, TPR_SHA256, save, sha

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "gromacs-mpinat-20261003"))
import qualify_candidate as native

TASK = "lynx-performance-20261005"
REMOTE = "/mnt/fs2-scientific/cpu-probe"


def pod_spec(args):
    if (not args.name.startswith("fs2-lynx-perf-cpu-") or "@sha256:" not in args.image
            or args.cpus not in (8, 16, 32)):
        raise ValueError("Require an immutable image and task-owned Pod name")
    resources = {"cpu": str(args.cpus), "memory": "16Gi", "nvidia.com/gpu": "1", "ephemeral-storage": "8Gi"}
    return {"apiVersion": "v1", "kind": "Pod", "metadata": {"name": args.name, "namespace": native.NS,
           "labels": {"scientific-ai.nebius.com/task": TASK}}, "spec": {
        "automountServiceAccountToken": False, "restartPolicy": "Never", "activeDeadlineSeconds": 1800,
        "nodeSelector": {"kubernetes.io/hostname": args.node},
        "tolerations": [{"key": "dedicated", "operator": "Equal", "value": "fs2-inference", "effect": "NoSchedule"}],
        "securityContext": {"runAsUser": 10001, "runAsGroup": 10001, "fsGroup": 10001},
        "containers": [{"name": "runtime", "image": args.image, "command": ["sleep", "1700"],
                        "resources": {"requests": resources, "limits": resources},
                        "securityContext": {"allowPrivilegeEscalation": False, "capabilities": {"drop": ["ALL"]}},
                        "volumeMounts": [{"name": "work", "mountPath": "/mnt/fs2-scientific"}]}],
        "volumes": [{"name": "work", "emptyDir": {"sizeLimit": "8Gi"}}]}}


def run(args):
    pod = pod_spec(args)
    if sha(args.source / "original.tpr") != TPR_SHA256:
        raise ValueError("Derived TPR must accompany the exact original input")
    native.KUBE = ["kubectl", "--context", args.context]
    args.output.mkdir(parents=True, exist_ok=False)
    capacity = native.free_capacity(args.node,
        json.loads(native.call(["get", "nodes", "-o", "json"]))["items"],
        json.loads(native.call(["get", "pods", "-A", "-o", "json"]))["items"], 1)
    save(args.output / "capacity-before.json", capacity)
    save(args.output / "pod-request.json", pod)
    request = json.dumps(pod).encode()
    native.call(["-n", native.NS, "create", "-f", "-", "--dry-run=client"], input=request)
    uid = None
    record = {"started_at": datetime.now(timezone.utc).isoformat(), "cpus": args.cpus, "gpus": 1,
              "image": args.image, "node": args.node, "pod": args.name,
              "original_tpr_sha256": TPR_SHA256, "finite_tpr_sha256": sha(args.source / "benchmark.tpr"),
              "finite_tpr_source": str(args.source), "public_shape_qualified": False,
              "protocol": "Native CLI fresh-cache plus three warm-cache 50000-step repeats, bondedGPU; no platform export."}
    try:
        made = json.loads(native.call(["-n", native.NS, "create", "-f", "-", "-o", "json"], input=request))
        uid = made["metadata"]["uid"]
        for _ in range(120):
            current = json.loads(native.call(["-n", native.NS, "get", "pod", args.name, "-o", "json"]))
            if any(c["type"] == "Ready" and c["status"] == "True" for c in current["status"].get("conditions", [])):
                break
            time.sleep(2)
        else:
            raise TimeoutError("Bounded native CPU Pod startup expired")
        record["image_id"] = current["status"]["containerStatuses"][0]["imageID"]
        manifest = json.loads(subprocess.check_output(["crane", "manifest", args.image], timeout=60))
        allowed = [args.image.split("@", 1)[1], *[m["digest"] for m in manifest.get("manifests", [])
                   if m.get("platform") == {"architecture": "amd64", "os": "linux"}]]
        if not any(d in record["image_id"] for d in allowed):
            raise ValueError("Running image differs from pinned CPU benchmark candidate")
        record["allowed_image_digests"] = allowed
        native.call(["-n", native.NS, "exec", args.name, "--", "mkdir", "-p", REMOTE])
        harness = ROOT / "models/molecular-dynamics/gromacs/qualification/benchmark_sm89.py"
        record["harness_sha256"] = sha(harness)
        for source, target in ((harness, "benchmark.py"), (args.source / "benchmark.tpr", "benchmark.tpr")):
            native.call(["-n", native.NS, "cp", "--no-preserve", str(source), args.name + ":" + REMOTE + "/" + target])
        command = ["python3", REMOTE + "/benchmark.py", "--tpr", REMOTE + "/benchmark.tpr",
                   "--sha256", record["finite_tpr_sha256"], "--output", REMOTE + "/results",
                   "--expected-steps", "50000", "--expected-time-ps", "100", "--threads", str(args.cpus),
                   "--bonded", "gpu"]
        record["command"] = command
        with (args.output / "worker.log").open("xb") as stream:
            proc = subprocess.run([*native.KUBE, "-n", native.NS, "exec", args.name, "--", *command],
                                  stdout=stream, stderr=subprocess.STDOUT, timeout=1200)
        record["exit_code"] = proc.returncode
        native.call(["-n", native.NS, "cp", args.name + ":" + REMOTE + "/results", str(args.output / "results")])
        summary = json.loads((args.output / "results/summary.json").read_text())
        measurements = json.loads((args.output / "results/measurements.json").read_text())
        record.update(summary=summary, passed=proc.returncode == 0 and summary["validated_warm_runs"] == 3
                      and all(r.get("validation", {}).get("passed") for r in measurements))
    except Exception as exc:
        record.update(passed=False, error=str(exc))
    finally:
        if uid:
            current = json.loads(native.call(["-n", native.NS, "get", "pod", args.name, "-o", "json"]))
            save(args.output / "pod-final.json", current)
            if current["metadata"]["uid"] != uid or current["metadata"]["labels"].get("scientific-ai.nebius.com/task") != TASK:
                raise ValueError("Refuse cleanup of a changed or unowned Pod")
            native.call(["-n", native.NS, "delete", "pod", args.name, "--wait=false"])
            for _ in range(30):
                if not native.call(["-n", native.NS, "get", "pod", args.name, "--ignore-not-found", "-o", "name"]).strip():
                    record["cleanup"] = "owned Pod deleted and absence observed"
                    break
                time.sleep(2)
        record["finished_at"] = datetime.now(timezone.utc).isoformat()
        save(args.output / "receipt.json", record)
    print(json.dumps(record), flush=True)
    return 0 if record.get("passed") and record.get("cleanup") else 1


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    for name in ("node", "name", "image"):
        p.add_argument("--" + name, required=True)
    for name in ("source", "output"):
        p.add_argument("--" + name, type=Path, required=True)
    p.add_argument("--cpus", type=int, choices=(8, 16, 32), required=True)
    p.add_argument("--context", default="nebius-mk8s-k8s-inference-h100-e00j5z9te7x5dd9g6a")
    raise SystemExit(run(p.parse_args()))
