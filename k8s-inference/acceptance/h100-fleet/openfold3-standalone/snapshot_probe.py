#!/usr/bin/env python3
"""Bounded standalone Preview2 snapshot probe using the frozen serving bridge."""
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


def utc():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def donor(source, image_config, args):
    identity = json.loads((HERE / "integration.json").read_bytes())
    runtime = next(item for item in source["spec"]["template"]["spec"]["containers"] if item["name"] == "model")
    if runtime["image"] != identity["image"] or image_config["image"] != identity["image"]:
        raise ValueError("The exact qualified standalone Preview2 image is required")
    bundle = json.loads((SNAPSHOTS / "qwen3-8b-bundle.json").read_bytes())
    uid, gid = (int(value) for value in image_config["user"].split(":"))
    config = argparse.Namespace(container="model", entrypoint_json=json.dumps(image_config["entrypoint"]),
        asyncio_loop=False, python="python3", run=args.run, fallback="fail", request_uid=uid,
        allow_device_remap=True, mode="donor", tools_image=bundle["tools_image"],
        model_revision=identity["model_revision"], model_id="openfold3",
        source_configmap=args.source_configmap, pvc=bundle["pvc"], node=args.node, name=args.pod)
    pod = recapture(initial(source, config), name=args.pod, container="model", run=args.run,
                    source_configmap=args.source_configmap, network_configmap=bundle["network_configmap"])
    runtime = next(item for item in pod["spec"]["containers"] if item["name"] == "model")
    separator = runtime["command"].index("--") + 1
    # Execute the literal OCI bash entrypoint after restoring its original cwd
    # and identity. The server uses stdlib HTTP, not an altered asyncio loop.
    runtime["command"][separator:separator] = ["python3", "/snapshot-source/working_directory_launcher.py",
        "--directory", image_config["working_directory"], "--uid", str(uid), "--gid", str(gid), "--"]
    # The exact image only provides Python inside its pinned conda environment.
    # The native bash activation still establishes the unchanged worker PATH.
    supervisor_path = next(item for item in runtime["env"] if item["name"] == "PATH")
    supervisor_path["value"] = "/opt/openfold3/.pixi/envs/openfold3-cuda12/bin:" + supervisor_path["value"]
    pod["metadata"]["labels"]["fs2.nebius/task"] = TASK
    pod["spec"]["activeDeadlineSeconds"] = 7200
    return pod


def validate(kube, pod, output):
    # This client uses the control-plane virtualenv, exactly as public_verify.
    spec = importlib.util.spec_from_file_location("of3_snapshot_original_public", HERE / "public_verify.py")
    public = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(public)
    contract, cases = public.cases_for("openfold3")
    output.mkdir(parents=True, exist_ok=True)
    with (output / "port-forward.log").open("wb") as log:
        forward = subprocess.Popen([*kube, "port-forward", "pod/" + pod, "19749:8000"], stdout=log, stderr=log)
        try:
            time.sleep(2)
            paths, rows = [], []
            for index, case in enumerate(cases):
                payload = public.PUBLIC.canonical_json(case.payload)
                started, tick = utc(), time.monotonic()
                with urlopen(Request("http://127.0.0.1:19749/biology/openfold/openfold3/predict", data=payload,
                                     headers={"Content-Type": "application/json"}), timeout=900) as response:
                    raw = response.read()
                path = output / f"response-{index}.json"
                path.write_bytes(raw)
                paths.append(path)
                rows.append({"id": public.REQUEST_IDS[index], "started_at": started, "finished_at": utc(),
                    "seconds": time.monotonic() - tick, "input_sha256": hashlib.sha256(payload).hexdigest(),
                    "response_sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw), "passed": True})
            checked = public.validate_pair("openfold3", contract, paths, output)
            result = {"passed": True, "model": "openfold3", "requests": rows, "validation": checked,
                "input_scope": "original two 20-aa request IDs, also used before capture; not unseen-input evidence"}
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
    parser.add_argument("--image-config", type=Path)
    parser.add_argument("--pod", default="fs2-mm-of3-snapshot-donor-r01")
    parser.add_argument("--run", default="of3-preview2-r01")
    parser.add_argument("--node", default="computeinstance-e00p3acr87k9k4mckj")
    parser.add_argument("--source-configmap", default="fs2-fleet-snapshot-serving-workdir-v8")
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
        bundle = json.loads((SNAPSHOTS / "qwen3-8b-bundle.json").read_bytes())
        pod = render(json.loads(args.source.read_bytes()), name=args.pod, container="model",
                     pvc=bundle["pvc"], address_configmap=bundle["address_configmap"],
                     address_python="/opt/openfold3/.pixi/envs/openfold3-cuda12/bin/python3")
        save("restore-manifest.json", pod)
        call(["create", "--dry-run=client", "-f", "-"], payload=json.dumps(pod))
        save("created.json", {"requested_at": utc(), "result": call(["create", "-f", "-"], payload=json.dumps(pod)).stdout})
        print(json.dumps({"pod": args.pod, "status": "created"}), flush=True)
        return

    if args.action == "create":
        pod = donor(json.loads(args.source.read_bytes()), json.loads(args.image_config.read_bytes()), args)
        save("donor-manifest.json", pod)
        call(["create", "--dry-run=client", "-f", "-"], payload=json.dumps(pod))
        save("created.json", {"requested_at": utc(), "result": call(["create", "-f", "-"], payload=json.dumps(pod)).stdout})
        print(json.dumps({"pod": args.pod, "status": "created"}), flush=True)
        return
    pod = json.loads(call(["get", "pod", args.pod, "-o", "json"]).stdout)
    if args.action != "validate" and pod["metadata"].get("labels", {}).get("fs2.nebius/task") != TASK:
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
            if any("terminated" in item["state"] for item in pod.get("status", {}).get("containerStatuses", [])):
                raise RuntimeError("Snapshot donor terminated before readiness")
            worker = call(["exec", args.pod, "-c", "model", "--", "tail", "-n", "250", f"/checkpoints/{args.run}/worker.log"], check=False)
            (args.output / "worker.log").write_text(worker.stdout + worker.stderr)
            ready = call(["exec", args.pod, "-c", "model", "--", "python3", "-c",
                "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8000/v1/health/ready',timeout=2).status)"], check=False)
            if ready.returncode == 0 and ready.stdout.strip() == "200":
                save("ready.json", {"pod": args.pod, "observed_at": utc()})
                print(json.dumps({"pod": args.pod, "status": "HTTP-ready", "observed_at": utc()}), flush=True)
                return
            time.sleep(2)
        raise TimeoutError("Snapshot donor readiness timed out")
    save("donor-completed.json", pod)
    code = "from pathlib import Path; import subprocess; d=Path('/checkpoints/" + args.run + "'); subprocess.run(['python3','/snapshot-source/serving_checkpoint.py','capture','--directory',str(d/'images'),'--pid',(d/'live-worker-pid').read_text().strip(),'--process-tree'],check=True)"
    result = call(["exec", args.pod, "-c", "model", "--", "python3", "-c", code], timeout=900, check=False)
    (args.output / "capture.log").write_text(result.stdout + result.stderr)
    if result.stdout.lstrip().startswith("{"):
        save("capture.json", json.JSONDecoder().raw_decode(result.stdout.lstrip())[0])
    for filename in ("compatibility.json", "dump.log"):
        item = call(["exec", args.pod, "-c", "model", "--", "cat", f"/checkpoints/{args.run}/images/{filename}"], check=False)
        (args.output / filename).write_text(item.stdout + item.stderr)
    result.check_returncode()
    print(json.dumps({"pod": args.pod, "status": "captured", "completed_at": utc()}), flush=True)


if __name__ == "__main__":
    main()
