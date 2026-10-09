"""Isolated native benchmark; never edits a production workload or node."""

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import sys
import time

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "lynx-performance-20261005"))
import native_cpu_probe as lifecycle

TASK = "l40s-incremental-20261006"
IMAGE = "cr.eu-north1.nebius.cloud/e00akg9ndpx77eaexh/fs2-platform/gromacs@sha256:ca863f44c7d17c8096267ec43939cda8b9546f0d3b11440e62bcf9149bc94a1e"
CUSTOMER_NODE = "computeinstance-e00xwjv9khjp8fhp3v"
CUSTOMER_POD = "fs2-workflow-mas1-20e-a1-ec69e0520fc9-fjr6j"
TPR_SHA256 = "e2ee73571f0dd9855709d2a957e41e5ad52316b3f1d2b1808e65476f4bd8ef10"
REMOTE = "/mnt/fs2-scientific/incremental"


@dataclass(frozen=True)
class ExperimentSpec:
    task: str = TASK
    pod_prefix: str = "fs2-lynx-perf-cpu-incremental-"
    allowed_pools: tuple = ("l40s-1x",)
    allowed_cpus: tuple = (8,)
    worker: Path = HERE / "experiment_inside.py"
    extra_sources: tuple = ()
    worker_args: tuple = ()
    purpose: str = "isolated native L40S benchmark"


DEFAULT_SPEC = ExperimentSpec()


def save(path, value):
    path.write_text(json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n")


def customer_observation(native):
    pod = json.loads(native.call(["-n", native.NS, "get", "pod", CUSTOMER_POD, "-o", "json"]))
    return {"observed_at": datetime.now(timezone.utc).isoformat(),
            "uid": pod["metadata"]["uid"], "node": pod["spec"]["nodeName"],
            "phase": pod["status"]["phase"],
            "containers": [{k: c.get(k) for k in ("name", "imageID", "restartCount", "ready")}
                           for c in pod["status"].get("containerStatuses", [])]}


def pod_spec(args, spec=DEFAULT_SPEC):
    if args.node == CUSTOMER_NODE or not args.name.startswith(spec.pod_prefix):
        raise ValueError("Customer node excluded; require unique task pod")
    args.cpus, args.image = getattr(args, "cpus", 8), IMAGE
    if args.cpus not in spec.allowed_cpus:
        raise ValueError("CPU envelope outside this experiment contract")
    pod = lifecycle.pod_spec(args)
    pod["metadata"]["labels"]["scientific-ai.nebius.com/task"] = spec.task
    pod["metadata"]["annotations"] = {"purpose": spec.purpose, "owner": "system/development"}
    pod["spec"]["activeDeadlineSeconds"] = 10800
    pod["spec"]["containers"][0]["command"] = ["sleep", "10700"]
    return pod


def run(args, spec=DEFAULT_SPEC):
    native = lifecycle.native
    native.KUBE = ["kubectl", "--context", args.context]
    if lifecycle.sha(args.tpr) != TPR_SHA256:
        raise ValueError("Original scientific input identity differs")
    args.output.mkdir(parents=True, exist_ok=False, mode=0o700)
    pod = pod_spec(args, spec)
    nodes = json.loads(native.call(["get", "nodes", "-o", "json"]))["items"]
    pods = json.loads(native.call(["get", "pods", "-A", "-o", "json"]))["items"]
    capacity = native.free_capacity(args.node, nodes, pods, 1)
    if capacity["labels"].get("accelerator.fs2.nebius/pool-id") not in spec.allowed_pools:
        raise ValueError("Node pool outside this experiment contract")
    for p in pods:
        if (p["spec"].get("nodeName") == args.node
                and p["status"].get("phase") not in ("Succeeded", "Failed")
                and not any(o["kind"] in ("DaemonSet", "Node") for o in p["metadata"].get("ownerReferences", []))):
            raise ValueError("Target has other application work; no eviction")
    record = {"mode": args.mode, "node": args.node, "pod": args.name, "image": IMAGE,
              "task": spec.task, "cpus": args.cpus, "gpus": 1,
              "input_sha256": TPR_SHA256, "started_at": datetime.now(timezone.utc).isoformat(),
              "customer_before": customer_observation(native), "capacity": capacity,
              "scope": "Native benchmark, not API or object-storage delivery acceptance"}
    save(args.output / "pod-request.json", pod)
    native.call(["-n", native.NS, "create", "-f", "-", "--dry-run=client"], input=json.dumps(pod).encode())
    uid = None
    try:
        made = json.loads(native.call(["-n", native.NS, "create", "-f", "-", "-o", "json"], input=json.dumps(pod).encode()))
        uid = made["metadata"]["uid"]
        record["pod_uid"] = uid
        save(args.output / "receipt-start.json", record)
        for _ in range(180):
            current = json.loads(native.call(["-n", native.NS, "get", "pod", args.name, "-o", "json"]))
            if any(c["type"] == "Ready" and c["status"] == "True" for c in current["status"].get("conditions", [])):
                break
            if current["status"].get("phase") == "Failed":
                raise RuntimeError("Task pod failed at startup")
            time.sleep(2)
        else:
            raise TimeoutError("Task pod startup exceeded six minutes")
        save(args.output / "pod-ready.json", current)
        record["image_id"] = current["status"]["containerStatuses"][0]["imageID"]
        if IMAGE.split("@", 1)[1] not in record["image_id"]:
            raise ValueError("Running worker differs from pinned production digest")
        native.call(["-n", native.NS, "exec", args.name, "--", "mkdir", "-p", REMOTE])
        sources = [(args.tpr, "original.tpr"), (spec.worker, spec.worker.name),
                   (lifecycle.ROOT / "models/molecular-dynamics/gromacs/qualification/benchmark_sm89.py", "benchmark_sm89.py"),
                   (HERE.parent / "lynx-l40s-final-20261005/screen_inside.py", "screen_inside.py"),
                   (Path("/home/tux/.codex/skills/gpu-performance/scripts/collect_blackwell_env.py"), "collect_env.py"),
                   *spec.extra_sources]
        record["source_hashes"] = {str(path): lifecycle.sha(path) for path, _ in sources}
        for source, target in sources:
            native.call(["-n", native.NS, "cp", "--no-preserve", str(source), args.name + ":" + REMOTE + "/" + target])
        if args.profile_tools:
            nsys = args.profile_tools / "target-linux-x64/nsys"
            if args.mode != "profile" or not nsys.is_file():
                raise ValueError("Profiler overlay is only allowed for an isolated profile run")
            record["profile_tool_identity"] = {
                "source": str(args.profile_tools), "nsys_sha256": lifecycle.sha(nsys)}
            subprocess.run([*native.KUBE, "-n", native.NS, "cp", "--no-preserve", str(args.profile_tools),
                            args.name + ":" + REMOTE + "/nsight"], check=True, capture_output=True, timeout=180)
        command = ["python3", REMOTE + "/" + spec.worker.name, "--mode", args.mode,
                   "--tpr", REMOTE + "/original.tpr", "--output", REMOTE + "/results", *spec.worker_args]
        record["command"] = command
        with (args.output / "experiment.log").open("xb") as log:
            process = subprocess.run([*native.KUBE, "-n", native.NS, "exec", args.name, "--", *command],
                                     stdout=log, stderr=subprocess.STDOUT, timeout=9900, check=False)
        record["exit_code"] = process.returncode
        native.call(["-n", native.NS, "cp", args.name + ":" + REMOTE + "/results", str(args.output / "results")])
        record["summary"] = json.loads((args.output / "results/summary.json").read_text())
        if process.returncode:
            record["status"] = "failed"
        elif args.mode == "profile" and record["summary"].get("status") != "captured":
            record["status"] = "incomplete"
        else:
            record["status"] = "passed"
    except Exception as exc:
        record.update(status="failed", error=str(exc))
    finally:
        if uid:
            current = json.loads(native.call(["-n", native.NS, "get", "pod", args.name, "-o", "json"]))
            save(args.output / "pod-final.json", current)
            if current["metadata"]["uid"] != uid or current["metadata"]["labels"].get("scientific-ai.nebius.com/task") != spec.task:
                raise ValueError("Refuse cleanup of unrelated/replaced resource")
            if record.get("status") != "passed":
                try:
                    native.call(["-n", native.NS, "cp", args.name + ":" + REMOTE + "/results", str(args.output / "partial-results")])
                except subprocess.SubprocessError:
                    pass
            native.call(["-n", native.NS, "delete", "pod", args.name, "--wait=false"])
            for _ in range(30):
                if not native.call(["-n", native.NS, "get", "pod", args.name, "--ignore-not-found", "-o", "name"]).strip():
                    record["cleanup"] = "exact task pod deleted; absence observed"
                    break
                time.sleep(2)
        record["customer_after"] = customer_observation(native)
        before, after = record["customer_before"], record["customer_after"]
        record["customer_unchanged"] = (before["uid"] == after["uid"] and before["node"] == after["node"]
            and before["containers"] == after["containers"] and after["phase"] == "Running")
        record["finished_at"] = datetime.now(timezone.utc).isoformat()
        save(args.output / "receipt.json", record)
    print(json.dumps({k: record[k] for k in ("status", "pod", "node", "cleanup", "customer_unchanged") if k in record}), flush=True)
    return 0 if record.get("status") == "passed" and record.get("cleanup") and record.get("customer_unchanged") else 1


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    for name in ("node", "name"):
        p.add_argument("--" + name, required=True)
    for name in ("tpr", "output"):
        p.add_argument("--" + name, type=Path, required=True)
    p.add_argument("--mode", choices=("cpu", "checkpoint", "profile", "pme", "wait-policy"), required=True)
    p.add_argument("--profile-tools", type=Path)
    p.add_argument("--context", default="nebius-mk8s-k8s-inference-h100-e00j5z9te7x5dd9g6a")
    raise SystemExit(run(p.parse_args()))
