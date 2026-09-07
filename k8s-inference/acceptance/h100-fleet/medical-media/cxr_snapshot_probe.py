#!/usr/bin/env python3
"""Task-only CXR snapshot capture using the unchanged generic serving bridge."""
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

ROOT = Path(__file__).resolve().parents[3]
SNAPSHOTS = ROOT / "acceptance/h100-fleet/snapshots"
sys.path.insert(0, str(SNAPSHOTS))
from render_serving_probe import render as initial  # noqa: E402
from render_serving_recapture import render as recapture  # noqa: E402


def utc():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def validate(kube, pod, output, container="vllm"):
    output.mkdir(parents=True, exist_ok=True)
    contract_path = ROOT / "catalog/runtime/validators/assets/nv-reason-cxr-3b.json"
    contract = json.loads(contract_path.read_text())
    spec = importlib.util.spec_from_file_location("original_cxr_validator", ROOT / "catalog/runtime/validators/validate_response.py")
    validator = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(validator)
    with (output / "port-forward.log").open("wb") as log:
        forward = subprocess.Popen([*kube, "port-forward", "pod/" + pod, "19747:8000"], stdout=log, stderr=log)
        try:
            time.sleep(2)
            paths, rows = [], []
            for index, item in enumerate(contract["requests"]):
                payload = json.dumps(item["wire_request"]).encode()
                started, tick = utc(), time.monotonic()
                with urlopen(Request("http://127.0.0.1:19747/v1/chat/completions", data=payload,
                                     headers={"Content-Type": "application/json"}), timeout=900) as response:
                    raw = response.read()
                path = output / f"response-{index}.json"
                path.write_bytes(raw)
                paths.append(path)
                rows.append({"id": item["id"], "started_at": started, "finished_at": utc(),
                             "seconds": time.monotonic() - tick, "input_sha256": hashlib.sha256(payload).hexdigest(),
                             "response_sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw), "passed": True})
            checked = validator.validate(contract, paths)
            result = {"passed": True, "model": "nv-reason-cxr-3b", "requests": rows,
                      "original_fixture_sha256": hashlib.sha256(contract_path.read_bytes()).hexdigest(),
                      "validation": checked, "input_scope": "original two pinned X-ray fixtures, also used before capture; not unseen-input evidence"}
            (output / "semantics.json").write_text(json.dumps(result, indent=2) + "\n")
            return result
        finally:
            forward.terminate()
            forward.wait(timeout=10)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("create", "restore", "observe", "validate", "capture", "delete"))
    parser.add_argument("--kubeconfig", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--source", type=Path)
    parser.add_argument("--pod", default="fs2-mm-cxr-snapshot-donor-r01")
    parser.add_argument("--run", default="cxr-r01")
    parser.add_argument("--node", default="computeinstance-e00p3acr87k9k4mckj")
    parser.add_argument("--capture-pvc", help="Existing task-only capture claim; default is shared snapshot filesystem")
    args = parser.parse_args()
    os.umask(0o077)
    args.output.mkdir(parents=True, exist_ok=True)
    kube = ["kubectl", "--kubeconfig", args.kubeconfig, "--context", "k8s-inference-h100", "-n", "fs2-models"]

    def call(command, *, payload=None, timeout=60, check=True):
        return subprocess.run([*kube, *command], input=payload, text=True, capture_output=True, check=check, timeout=timeout)

    def save(name, value):
        (args.output / name).write_text(json.dumps(value, indent=2) + "\n")

    if args.action == "restore":
        from render_readonly_server_restore import render
        bundle = json.loads((SNAPSHOTS / "qwen3-8b-bundle.json").read_text())
        pod = render(json.loads(args.source.read_bytes()), name=args.pod, container="vllm",
                     pvc=bundle["pvc"], address_configmap=bundle["address_configmap"])
        save("restore-manifest.json", pod)
        call(["create", "--dry-run=client", "-f", "-"], payload=json.dumps(pod))
        save("created.json", {"requested_at": utc(), "result": call(["create", "-f", "-"], payload=json.dumps(pod)).stdout})
        print(json.dumps({"pod": args.pod, "status": "created"}), flush=True)
        return

    if args.action == "create":
        bundle = json.loads((SNAPSHOTS / "qwen3-8b-bundle.json").read_text())
        config = argparse.Namespace(container="vllm", entrypoint_json=json.dumps(bundle["image_entrypoint"]),
            asyncio_loop=True, python="python3", run=args.run, fallback="fail", request_uid=None,
            allow_device_remap=True, mode="donor", tools_image=bundle["tools_image"],
            model_revision="056bd0383b35226554da9dc5866e095df174ae19", model_id="nv-reason-cxr-3b",
            source_configmap=bundle["source_configmap"], pvc=args.capture_pvc or bundle["pvc"], node=args.node, name=args.pod)
        source = json.loads(args.source.read_text())
        pod = initial(source, config)
        pod = recapture(pod, name=args.pod, container="vllm", run=args.run,
                        source_configmap=bundle["source_configmap"], network_configmap=bundle["network_configmap"])
        pod["metadata"]["labels"]["fs2.nebius/task"] = "fs2-h100-fleet-medical-media-r20260907"
        pod["spec"]["activeDeadlineSeconds"] = 7200
        # Never let kubelet re-own a shared volume containing immutable bundles.
        # The snapshot supervisor is root; the existing group remains supplemental.
        pod["spec"].setdefault("securityContext", {}).pop("fsGroup", None)
        pod["spec"]["securityContext"].pop("fsGroupChangePolicy", None)
        pod["spec"]["securityContext"]["supplementalGroups"] = [1000]
        pod["spec"]["containers"][0]["env"].append({"name": "USE_LIBUV", "value": "0"})
        save("donor-manifest.json", pod)
        call(["create", "--dry-run=client", "-f", "-"], payload=json.dumps(pod))
        save("created.json", {"requested_at": utc(), "result": call(["create", "-f", "-"], payload=json.dumps(pod)).stdout})
        print(json.dumps({"pod": args.pod, "status": "created"}), flush=True)
        return
    pod = json.loads(call(["get", "pod", args.pod, "-o", "json"]).stdout)
    if args.action != "validate" and pod["metadata"].get("labels", {}).get("fs2.nebius/task") != "fs2-h100-fleet-medical-media-r20260907":
        raise ValueError("Task Pod ownership mismatch")
    if args.action == "delete":
        result = call(["delete", "pod", args.pod, "--wait=true", "--timeout=60s"])
        save("deleted.json", {"uid": pod["metadata"]["uid"], "completed_at": utc(), "result": result.stdout})
        return
    if args.action == "validate":
        print(json.dumps(validate(kube, args.pod, args.output)), flush=True)
        return
    if args.action == "observe":
        for _ in range(450):
            pod = json.loads(call(["get", "pod", args.pod, "-o", "json"]).stdout)
            save("donor-live.json", pod)
            for container in pod.get("status", {}).get("containerStatuses", []):
                if "terminated" in container["state"]:
                    raise RuntimeError("Snapshot donor exited before application readiness")
            worker = call(["exec", args.pod, "-c", "vllm", "--", "tail", "-n", "250", f"/checkpoints/{args.run}/worker.log"], check=False)
            (args.output / "worker.log").write_text(worker.stdout + worker.stderr)
            probe = call(["exec", args.pod, "-c", "vllm", "--", "python3", "-c",
                          "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8000/health',timeout=2).status)"], check=False)
            if probe.returncode == 0 and probe.stdout.strip() == "200":
                save("ready.json", {"pod": args.pod, "observed_at": utc()})
                print(json.dumps({"pod": args.pod, "status": "HTTP-ready", "observed_at": utc()}), flush=True)
                return
            time.sleep(2)
        raise TimeoutError("Donor did not reach application readiness")
    save("donor-completed.json", pod)
    code = "from pathlib import Path; import subprocess; d=Path('/checkpoints/" + args.run + "'); subprocess.run(['python3','/snapshot-source/serving_checkpoint.py','capture','--directory',str(d/'images'),'--pid',(d/'live-worker-pid').read_text().strip(),'--process-tree'],check=True)"
    result = call(["exec", args.pod, "-c", "vllm", "--", "python3", "-c", code], timeout=900, check=False)
    (args.output / "capture.log").write_text(result.stdout + result.stderr)
    if result.stdout.lstrip().startswith("{"):
        save("capture.json", json.JSONDecoder().raw_decode(result.stdout.lstrip())[0])
    for filename in ("compatibility.json", "dump.log"):
        result_file = call(["exec", args.pod, "-c", "vllm", "--", "cat", f"/checkpoints/{args.run}/images/{filename}"], check=False)
        (args.output / filename).write_text(result_file.stdout + result_file.stderr)
    result.check_returncode()
    print(json.dumps({"pod": args.pod, "status": "captured", "completed_at": utc()}), flush=True)


if __name__ == "__main__":
    main()
