"""Run an isolated MPS comparison on one currently free existing GPU.

Uses the existing native qualification capacity/image/ownership helpers. No
customer API key, node mode change, shared MPS service or public shape change.
"""

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import sys
import time

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "lynx-performance-20261005"))
import native_cpu_probe as helper  # noqa: E402

native = helper.native
TASK = "lynx-mps-20261005"
REMOTE = "/mnt/fs2-scientific/mps-probe"


def pod_spec(args):
    pod = helper.pod_spec(args)
    pod["metadata"]["labels"]["scientific-ai.nebius.com/task"] = TASK
    pod["metadata"]["labels"]["scientific-ai.nebius.com/tenant"] = "system"
    pod["spec"]["activeDeadlineSeconds"] = 5400
    pod["spec"]["containers"][0]["command"] = ["sleep", "5300"]
    # This is one whole GPU, not advertised virtual shares. The private MPS
    # daemon and all client processes run within that exact reservation.
    return pod


def main(args):
    if helper.sha(args.source / "original.tpr") != helper.TPR_SHA256:
        raise ValueError("Finite fixture must accompany the original exact Lynx input")
    pod = pod_spec(args)
    native.KUBE = ["kubectl", "--context", args.context]
    args.output.mkdir(parents=True, exist_ok=False)
    capacity = native.free_capacity(args.node,
        json.loads(native.call(["get", "nodes", "-o", "json"]))["items"],
        json.loads(native.call(["get", "pods", "-A", "-o", "json"]))["items"], 1)
    helper.save(args.output / "capacity-before.json", capacity)
    helper.save(args.output / "pod-request.json", pod)
    record = {"started_at": datetime.now(timezone.utc).isoformat(), "node": args.node, "pod": args.name,
              "context": args.context, "namespace": native.NS, "image": args.image,
              "original_tpr_sha256": helper.TPR_SHA256,
              "finite_tpr_sha256": helper.sha(args.source / "benchmark.tpr"),
              "gpus": 1, "cpus": 8, "ram_gib": 16, "public_sharing_qualified": False,
              "task": TASK, "source_directory": str(args.source)}
    uid = None
    try:
        native.call(["-n", native.NS, "create", "-f", "-", "--dry-run=server"], input=json.dumps(pod).encode())
        made = json.loads(native.call(["-n", native.NS, "create", "-f", "-", "-o", "json"],
                                      input=json.dumps(pod).encode()))
        uid = made["metadata"]["uid"]
        record["pod_uid"] = uid
        for _ in range(150):
            current = json.loads(native.call(["-n", native.NS, "get", "pod", args.name, "-o", "json"]))
            if any(c["type"] == "Ready" and c["status"] == "True" for c in current["status"].get("conditions", [])):
                break
            time.sleep(2)
        else:
            raise TimeoutError("Isolated MPS Pod startup exceeded five minutes")
        record["image_id"] = current["status"]["containerStatuses"][0]["imageID"]
        manifest = json.loads(subprocess.check_output(["crane", "manifest", args.image], timeout=60))
        allowed = [args.image.split("@", 1)[1], *[m["digest"] for m in manifest.get("manifests", [])
                   if m.get("platform") == {"architecture": "amd64", "os": "linux"}]]
        if not any(digest in record["image_id"] for digest in allowed):
            raise ValueError("Observed worker differs from pinned engine image")
        native.call(["-n", native.NS, "exec", args.name, "--", "mkdir", "-p", REMOTE])
        sources = ((HERE / "benchmark_mps.py", "benchmark_mps.py"),
                   (helper.ROOT / "models/molecular-dynamics/gromacs/qualification/benchmark_sm89.py", "benchmark_sm89.py"),
                   (args.source / "benchmark.tpr", "benchmark.tpr"))
        record["uploaded_sources"] = []
        for source, target in sources:
            native.call(["-n", native.NS, "cp", "--no-preserve", str(source), args.name + ":" + REMOTE + "/" + target])
            record["uploaded_sources"].append({"name": target, "sha256": helper.sha(source)})
        command = ["python3", REMOTE + "/benchmark_mps.py", "--tpr", REMOTE + "/benchmark.tpr",
                   "--sha256", record["finite_tpr_sha256"], "--output", REMOTE + "/results",
                   "--steps", "50000", "--repetitions", str(args.repetitions)]
        record["command"] = command
        helper.save(args.output / "started.json", record)
        with (args.output / "worker.log").open("xb") as stream:
            completed = subprocess.run([*native.KUBE, "-n", native.NS, "exec", args.name, "--", *command],
                                       stdout=stream, stderr=subprocess.STDOUT, timeout=5100)
        record["exit_code"] = completed.returncode
        native.call(["-n", native.NS, "cp", args.name + ":" + REMOTE + "/results", str(args.output / "results")])
        if (args.output / "results/summary.json").exists():
            record["summary"] = json.loads((args.output / "results/summary.json").read_text())
        record["passed"] = completed.returncode == 0 and record.get("summary", {}).get("passed", False)
    except Exception as error:
        record.update(passed=False, error=str(error))
        if uid:
            # Retain failed benchmark evidence before deleting only our Pod.
            try:
                native.call(["-n", native.NS, "cp", args.name + ":" + REMOTE, str(args.output / "failed-workspace")])
            except subprocess.SubprocessError:
                record["failed_workspace_capture"] = "unavailable"
    finally:
        if uid:
            current = json.loads(native.call(["-n", native.NS, "get", "pod", args.name, "-o", "json"]))
            helper.save(args.output / "pod-final.json", current)
            if current["metadata"]["uid"] != uid or current["metadata"]["labels"].get("scientific-ai.nebius.com/task") != TASK:
                raise ValueError("Refuse deletion of a changed or unowned Pod")
            native.call(["-n", native.NS, "delete", "pod", args.name, "--wait=false"])
            for _ in range(30):
                if not native.call(["-n", native.NS, "get", "pod", args.name, "--ignore-not-found", "-o", "name"]).strip():
                    record["cleanup"] = "exact task Pod deleted and absence observed"
                    break
                time.sleep(2)
        record["finished_at"] = datetime.now(timezone.utc).isoformat()
        helper.save(args.output / "receipt.json", record)
    print(json.dumps(record), flush=True)
    return 0 if record.get("passed") and record.get("cleanup") else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("node", "name", "image"):
        parser.add_argument("--" + name, required=True)
    for name in ("source", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--cpus", type=int, choices=(8,), default=8)
    parser.add_argument("--repetitions", type=int, choices=(1, 2, 3), default=3)
    parser.add_argument("--context", default="nebius-mk8s-k8s-inference-h100-e00j5z9te7x5dd9g6a")
    raise SystemExit(main(parser.parse_args()))
