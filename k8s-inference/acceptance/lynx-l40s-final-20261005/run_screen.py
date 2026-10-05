"""Own one bounded native L40S Pod; no API key, demo lane or node policy writes."""

import argparse
import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

BASE = Path(__file__).resolve().parents[1] / "lynx-performance-20261005"
sys.path.insert(0, str(BASE))
import native_cpu_probe as lifecycle
from recipes import ROOT, TPR_SHA256, save, sha

TASK = "lynx-l40s-final-20261005"
IMAGE = "cr.eu-north1.nebius.cloud/e00akg9ndpx77eaexh/fs2-platform/gromacs@sha256:5acd77d66257593896c2fe09cac15392adfffaf00fc7f7625d228a6c8d6084dd"
REMOTE = "/mnt/fs2-scientific/final-l40s"


def pod_spec(args):
    if args.image != IMAGE or args.cpus != 8 or not args.name.startswith("fs2-lynx-perf-cpu-final-"):
        raise ValueError("Only this exact existing worker and one eight-CPU task Pod")
    pod = lifecycle.pod_spec(args)
    pod["metadata"]["labels"]["scientific-ai.nebius.com/task"] = TASK
    pod["spec"]["activeDeadlineSeconds"] = 2400
    pod["spec"]["containers"][0]["command"] = ["sleep", "2300"]
    return pod


def run(args):
    if sha(args.tpr) != TPR_SHA256:
        raise ValueError("Not the approved original TPR")
    native = lifecycle.native
    native.KUBE = ["kubectl", "--context", args.context]
    args.output.mkdir(parents=True, exist_ok=False)
    nodes = json.loads(native.call(["get", "nodes", "-o", "json"]))["items"]
    pods = json.loads(native.call(["get", "pods", "-A", "-o", "json"]))["items"]
    capacity = native.free_capacity(args.node, nodes, pods, 1)
    if capacity["labels"].get("accelerator.fs2.nebius/pool-id") != "l40s-1x":
        raise ValueError("Only the reviewed dedicated single-L40S pool")
    for p in pods:
        if (p["spec"].get("nodeName") == args.node
                and p["status"].get("phase") not in ("Succeeded", "Failed")
                and not any(owner["kind"] in ("DaemonSet", "Node") for owner in p["metadata"].get("ownerReferences", []))):
            raise ValueError("Dedicated native target acquired other application work")
    pod = pod_spec(args)
    save(args.output / "capacity-before.json", capacity)
    save(args.output / "pod-request.json", pod)
    native.call(["-n", native.NS, "create", "-f", "-", "--dry-run=client"], input=json.dumps(pod).encode())
    record = {"started_at": datetime.now(timezone.utc).isoformat(), "image": args.image,
              "node": args.node, "pod": args.name, "gpus": 1, "cpus": 8, "memory": "16Gi",
              "original_tpr_sha256": TPR_SHA256, "customer_path_tested": False}
    uid = None
    try:
        made = json.loads(native.call(["-n", native.NS, "create", "-f", "-", "-o", "json"], input=json.dumps(pod).encode()))
        uid = made["metadata"]["uid"]
        record["pod_uid"] = uid
        for _ in range(120):
            current = json.loads(native.call(["-n", native.NS, "get", "pod", args.name, "-o", "json"]))
            if any(row["type"] == "Ready" and row["status"] == "True" for row in current["status"].get("conditions", [])):
                break
            time.sleep(2)
        else:
            raise TimeoutError("Bounded native Pod startup expired")
        save(args.output / "pod-ready.json", current)
        image_id = current["status"]["containerStatuses"][0]["imageID"]
        manifest = json.loads(subprocess.check_output(["crane", "manifest", args.image], timeout=60))
        allowed = [args.image.split("@", 1)[1], *[m["digest"] for m in manifest.get("manifests", [])
                   if m.get("platform") == {"architecture": "amd64", "os": "linux"}]]
        if not any(digest in image_id for digest in allowed):
            raise ValueError("Native imageID differs from pinned worker")
        record.update(image_id=image_id, allowed_image_digests=allowed)
        native.call(["-n", native.NS, "exec", args.name, "--", "mkdir", "-p", REMOTE])
        sources = [(args.tpr, "original.tpr"), (Path(__file__).with_name("screen_inside.py"), "screen_inside.py"),
                   (ROOT / "models/molecular-dynamics/gromacs/qualification/benchmark_sm89.py", "benchmark_sm89.py"),
                   (Path("/home/tux/.codex/skills/gpu-performance/scripts/collect_blackwell_env.py"), "collect_env.py")]
        record["source_hashes"] = {str(path): sha(path) for path, _ in sources}
        for source, target in sources:
            native.call(["-n", native.NS, "cp", "--no-preserve", str(source), args.name + ":" + REMOTE + "/" + target])
        native.call(["-n", native.NS, "exec", args.name, "--", "python3", REMOTE + "/collect_env.py", "--output", REMOTE + "/environment.json"])
        command = ["python3", REMOTE + "/screen_inside.py", "--tpr", REMOTE + "/original.tpr", "--output", REMOTE + "/results"]
        if args.public_pin_probe:
            command.append("--public-pin-probe")
        record["command"] = command
        with (args.output / "screen.log").open("xb") as log:
            process = subprocess.run([*native.KUBE, "-n", native.NS, "exec", args.name, "--", *command],
                                     stdout=log, stderr=subprocess.STDOUT, timeout=2100, check=False)
        record["exit_code"] = process.returncode
        native.call(["-n", native.NS, "cp", args.name + ":" + REMOTE + "/results", str(args.output / "results")])
        native.call(["-n", native.NS, "cp", args.name + ":" + REMOTE + "/environment.json", str(args.output / "environment.json")])
        if process.returncode:
            raise ValueError("Native screen failed; original results retained")
        record["summary"] = json.loads((args.output / "results/summary.json").read_text())
        checked = 0
        for path in (args.output / "results").glob("*/measurements.json"):
            for row in json.loads(path.read_text()):
                for item in row["output_inventory"]:
                    output = path.parent / row["cohort"] / item["name"]
                    if output.stat().st_size != item["bytes"] or sha(output) != item["sha256"]:
                        raise ValueError("Retained native inventory hash differs")
                    checked += 1
                if row.get("validation", {}).get("passed"):
                    native.validate_gro(path.parent / row["cohort"] / "md.gro", 185486)
        record.update(status="passed", independently_rehashed_files=checked)
    except Exception as exc:  # noqa: BLE001 -- retain every local failure before UID-fenced cleanup
        record.update(status="failed", error=str(exc))
    finally:
        if uid:
            current = json.loads(native.call(["-n", native.NS, "get", "pod", args.name, "-o", "json"]))
            save(args.output / "pod-final.json", current)
            if current["metadata"]["uid"] != uid or current["metadata"]["labels"].get("scientific-ai.nebius.com/task") != TASK:
                raise ValueError("Refuse cleanup of a changed or unrelated Pod")
            native.call(["-n", native.NS, "delete", "pod", args.name, "--wait=false"])
            for _ in range(30):
                if not native.call(["-n", native.NS, "get", "pod", args.name, "--ignore-not-found", "-o", "name"]).strip():
                    record["cleanup"] = "exact owned Pod deleted; absence observed"
                    break
                time.sleep(2)
        record["finished_at"] = datetime.now(timezone.utc).isoformat()
        save(args.output / "receipt.json", record)
    print(json.dumps(record), flush=True)
    return 0 if record.get("status") == "passed" and record.get("cleanup") else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--node", required=True)
    parser.add_argument("--name", required=True)
    parser.add_argument("--tpr", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--image", default=IMAGE)
    parser.add_argument("--cpus", type=int, default=8)
    parser.add_argument("--public-pin-probe", action="store_true")
    parser.add_argument("--context", default="nebius-mk8s-k8s-inference-h100-e00j5z9te7x5dd9g6a")
    raise SystemExit(run(parser.parse_args()))
