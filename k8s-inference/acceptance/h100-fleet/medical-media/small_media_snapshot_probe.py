#!/usr/bin/env python3
"""Exact-image SDXL/Segment probes using the frozen optional serving bridge."""
import argparse
import datetime as dt
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from urllib.request import Request, urlopen

HERE = Path(__file__).resolve().parent
SNAPSHOTS = HERE.parent / "snapshots"
TASK = "fs2-h100-fleet-medical-media-r20260907"
sys.path.insert(0, str(SNAPSHOTS))
from render_serving_probe import render as initial  # noqa: E402
from render_serving_recapture import render as recapture  # noqa: E402
from render_readonly_server_restore import render as restore  # noqa: E402


def utc():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def donor(source, args):
    identity = json.loads((HERE / "integration.json").read_bytes())["models"][args.model]
    spec = source["spec"]["template"]["spec"]
    original = next(item for item in spec["containers"] if item["name"] == "server")
    if original["image"] != identity["image"]:
        raise ValueError("Exact qualified image required")
    uid = spec["securityContext"]["runAsUser"]
    gid = spec["securityContext"]["runAsGroup"]
    if uid != gid or uid != 1000:
        raise ValueError("Original measured media identity differs")
    bundle = json.loads((SNAPSHOTS / "qwen3-8b-bundle.json").read_bytes())
    config = argparse.Namespace(container="server", entrypoint_json=json.dumps(original["command"]),
        asyncio_loop=False, python="python3", run=args.run, fallback="fail", request_uid=uid,
        allow_device_remap=True, mode="donor", tools_image=bundle["tools_image"],
        model_revision=identity["model_revision"], model_id=args.model,
        source_configmap=args.source_configmap, pvc=bundle["pvc"], node=args.node, name=args.pod)
    pod = recapture(initial(source, config), name=args.pod, container="server", run=args.run,
                    source_configmap=args.source_configmap, network_configmap=bundle["network_configmap"])
    runtime = next(item for item in pod["spec"]["containers"] if item["name"] == "server")
    offset = runtime["command"].index("--") + 1
    runtime["command"][offset:offset] = ["python3", "/snapshot-source/working_directory_launcher.py",
        "--directory", "/vllm-workspace", "--uid", str(uid), "--gid", str(gid), "--"]
    pod["metadata"]["labels"]["fs2.nebius/task"] = TASK
    pod["spec"]["activeDeadlineSeconds"] = 7200
    return pod


def validate(kube, pod, model, output):
    spec = importlib.util.spec_from_file_location("original_media_public", HERE / "public_verify.py")
    public = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(public)
    contract, cases = public.cases_for(model)
    output.mkdir(parents=True, exist_ok=True)
    endpoint = "/segment" if model == "nv-segment-ct" else "/generate"
    with (output / "port-forward.log").open("wb") as log:
        forward = subprocess.Popen([*kube, "port-forward", "pod/" + pod, "19751:8000"], stdout=log, stderr=log)
        try:
            time.sleep(2)
            rows, paths = [], []
            for index, case in enumerate(cases):
                payload = public.canonical_json(case.payload)
                started, tick = utc(), time.monotonic()
                with urlopen(Request("http://127.0.0.1:19751" + endpoint, data=payload,
                                     headers={"Content-Type": "application/json"}), timeout=900) as response:
                    raw = response.read()
                path = output / f"response-{index}.json"
                path.write_bytes(raw)
                paths.append(path)
                rows.append({"id": contract["requests"][index]["id"], "started_at": started,
                    "finished_at": utc(), "seconds": time.monotonic() - tick,
                    "input_sha256": hashlib.sha256(payload).hexdigest(),
                    "response_sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw), "passed": True})
            result = {"passed": True, "model": model, "requests": rows,
                "validation": public.validate_pair(model, contract, paths, output),
                "input_scope": "two original pinned semantic requests, also used before capture; not unseen-input evidence"}
            (output / "semantics.json").write_text(json.dumps(result, indent=2) + "\n")
            return result
        finally:
            forward.terminate()
            forward.wait(timeout=10)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("create", "restore", "observe", "validate", "capture", "delete"))
    parser.add_argument("--model", choices=("sdxl", "nv-segment-ct"), required=True)
    parser.add_argument("--pod", required=True)
    parser.add_argument("--run")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--source", type=Path)
    parser.add_argument("--node", default="computeinstance-e00p3acr87k9k4mckj")
    parser.add_argument("--source-configmap", default="fs2-fleet-snapshot-serving-workdir-v8")
    parser.add_argument("--kubeconfig", required=True)
    args = parser.parse_args()
    if args.action in ("create", "observe", "capture") and not args.run:
        parser.error("--run is required for create, observe and capture")
    os.umask(0o077)
    args.output.mkdir(parents=True, exist_ok=True)
    kube = ["kubectl", "--kubeconfig", args.kubeconfig, "--context", "k8s-inference-h100", "-n", "fs2-models"]

    def call(command, *, payload=None, check=True, timeout=60):
        return subprocess.run([*kube, *command], input=payload, text=True, capture_output=True, check=check, timeout=timeout)

    def save(name, value):
        (args.output / name).write_text(json.dumps(value, indent=2) + "\n")

    if args.action in ("create", "restore"):
        source = json.loads(args.source.read_bytes())
        if args.action == "create":
            pod = donor(source, args)
        else:
            bundle = json.loads((SNAPSHOTS / "qwen3-8b-bundle.json").read_bytes())
            pod = restore(source, name=args.pod, container="server", pvc=bundle["pvc"],
                          address_configmap=bundle["address_configmap"])
        save("pod-manifest.json", pod)
        call(["create", "--dry-run=client", "-f", "-"], payload=json.dumps(pod))
        save("created.json", {"requested_at": utc(), "result": call(["create", "-f", "-"], payload=json.dumps(pod)).stdout})
        print(json.dumps({"status": "created", "pod": args.pod}), flush=True)
        return
    pod = json.loads(call(["get", "pod", args.pod, "-o", "json"]).stdout)
    if args.action != "validate" and pod["metadata"].get("labels", {}).get("fs2.nebius/task") != TASK:
        raise ValueError("Task ownership mismatch")
    if args.action == "delete":
        call(["delete", "pod", args.pod, "--wait=true", "--timeout=60s"])
        save("deleted.json", {"uid": pod["metadata"]["uid"], "completed_at": utc()})
        return
    if args.action == "validate":
        print(json.dumps(validate(kube, args.pod, args.model, args.output)), flush=True)
        return
    if args.action == "observe":
        for _ in range(300):
            pod = json.loads(call(["get", "pod", args.pod, "-o", "json"]).stdout)
            save("pod-private.json", pod)
            if any("terminated" in item["state"] for item in pod.get("status", {}).get("containerStatuses", [])):
                raise RuntimeError("Donor exited before application readiness")
            logs = call(["exec", args.pod, "-c", "server", "--", "tail", "-n", "250", f"/checkpoints/{args.run}/worker.log"], check=False)
            (args.output / "worker.log").write_text(logs.stdout + logs.stderr)
            result = call(["exec", args.pod, "-c", "server", "--", "python3", "-c",
                "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8000/readyz',timeout=2).status)"], check=False)
            if result.returncode == 0 and result.stdout.strip() == "200":
                save("ready.json", {"pod": args.pod, "observed_at": utc()})
                print(json.dumps({"pod": args.pod, "status": "HTTP-ready", "observed_at": utc()}), flush=True)
                return
            time.sleep(2)
        raise TimeoutError("Donor readiness timeout")
    save("donor-completed.json", pod)
    code = "from pathlib import Path; import subprocess; d=Path('/checkpoints/" + args.run + "'); subprocess.run(['python3','/snapshot-source/serving_checkpoint.py','capture','--directory',str(d/'images'),'--pid',(d/'live-worker-pid').read_text().strip(),'--process-tree'],check=True)"
    result = call(["exec", args.pod, "-c", "server", "--", "python3", "-c", code], timeout=900, check=False)
    (args.output / "capture.log").write_text(result.stdout + result.stderr)
    if result.stdout.lstrip().startswith("{"):
        save("capture.json", json.JSONDecoder().raw_decode(result.stdout.lstrip())[0])
    for filename in ("compatibility.json", "dump.log"):
        receipt = call(["exec", args.pod, "-c", "server", "--", "cat", f"/checkpoints/{args.run}/images/{filename}"], check=False)
        (args.output / filename).write_text(receipt.stdout + receipt.stderr)
    result.check_returncode()
    print(json.dumps({"pod": args.pod, "status": "captured", "completed_at": utc()}), flush=True)


if __name__ == "__main__":
    main()
