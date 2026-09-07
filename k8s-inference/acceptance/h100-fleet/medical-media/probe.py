#!/usr/bin/env python3
"""Isolated H100 copies of the four retained medical/media runtimes.

No production Deployment, Service or pool is changed. Each fresh Pod reuses its
own retained task PVC. Raw responses stay in the caller's private output root.
"""
from __future__ import annotations

import argparse
import copy
import datetime as dt
import hashlib
import importlib.util
import json
import subprocess
import time
from pathlib import Path
from urllib.request import Request, urlopen

import yaml

ROOT = Path(__file__).resolve().parents[3]
TASK = "fs2-h100-fleet-medical-media-r20260907"
MODELS = ("sdxl", "nv-segment-ct", "nv-reason-cxr-3b", "evo2-40b")
REGISTRY = "cr.eu-north1.nebius.cloud/e00akg9ndpx77eaexh/fs2-models"


def utc():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def write(path, value):
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def module(name):
    path = ROOT / "catalog/runtime/validators" / (name + ".py")
    spec = importlib.util.spec_from_file_location(name, path)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


def run(args):
    if args.model == "evo2-40b" and args.action in ("create", "observe", "validate") and (not args.image or "@sha256:" not in args.image):
        raise ValueError("Evo2 H100 requires the qualified immutable successor --image")
    args.output.mkdir(parents=True, exist_ok=True, mode=0o700)
    k = ["kubectl", "--kubeconfig", args.kubeconfig, "--context", args.context,
         "--namespace", "fs2-models"]

    def kube(*words, data=None):
        return subprocess.run(k + list(words), input=data, text=True,
                              capture_output=True, check=True).stdout

    name = f"fs2-mm-{args.model}-r{args.repetition:02d}-20260907"
    existing_cache = getattr(args, "existing_cache_pvc", None)
    cache = existing_cache or f"fs2-mm-{args.model}-cache-20260907"
    documents = list(yaml.safe_load_all((ROOT / "models/general-media/k8s" / (args.model + ".yaml")).read_text()))
    deployment = next(d for d in documents if d["kind"] == "Deployment")
    source_pvc = next(d for d in documents if d["kind"] == "PersistentVolumeClaim")
    pvc = copy.deepcopy(source_pvc)
    pvc["metadata"] = {"name": cache, "namespace": "fs2-models", "labels": {"fs2.nebius/task": TASK}}
    # Mounted-fs RWX claims all share the small fs2cache filesystem. Evo2's
    # 82 GB checkpoint needs its own block volume, including merge headroom.
    pvc["spec"]["storageClassName"] = "compute-csi-default-sc" if args.model == "evo2-40b" else "csi-mounted-fs-path-sc"
    pvc["spec"]["accessModes"] = ["ReadWriteOnce"] if args.model == "evo2-40b" else ["ReadWriteMany"]
    if existing_cache:
        # The release owner creates the destination claim with Terraform. A
        # qualification probe may consume it, but never apply a replacement.
        pvc = json.loads(kube("get", "pvc", existing_cache, "-o", "json"))
        if pvc.get("status", {}).get("phase") != "Bound":
            raise ValueError("Existing cache PVC must already be Bound")
    pod = {"apiVersion": "v1", "kind": "Pod", "metadata": {
        "name": name, "namespace": "fs2-models", "labels": {"fs2.nebius/task": TASK,
        "fs2.nebius/probe-model": args.model}, "annotations": copy.deepcopy(deployment["spec"]["template"]["metadata"].get("annotations", {}))},
        "spec": copy.deepcopy(deployment["spec"]["template"]["spec"])}
    spec = pod["spec"]
    spec.pop("serviceAccountName", None)
    spec["restartPolicy"] = "Never"
    spec["nodeSelector"] = {"accelerator.fs2.nebius/class": "nvidia-h100-sxm5-80gb",
                            "accelerator.fs2.nebius/pool-id": "h100-reserved-8x"}
    if getattr(args, "node", None):
        spec["nodeSelector"]["kubernetes.io/hostname"] = args.node
    for v in spec.get("volumes", []):
        if "persistentVolumeClaim" in v:
            v["persistentVolumeClaim"]["claimName"] = cache
    for c in spec.get("initContainers", []) + spec["containers"]:
        c["image"] = c["image"].replace("registry.example.invalid/k8s-inference/models", REGISTRY)
        for e in c.get("env", []):
            if "value" in e:
                e["value"] = e["value"].replace("driver-580.173.02-sm103", "driver-580.159.04-sm90").replace("deployment-profile-abi-v1", "driver-580.159.04-sm90")
        if args.model == "evo2-40b" and c["name"] == "model":
            c["env"] = [e for e in c["env"] if e["name"] != "CUDA_VISIBLE_DEVICES"]
            c["resources"]["requests"]["nvidia.com/gpu"] = "2"
            c["resources"]["limits"]["nvidia.com/gpu"] = "2"
        if args.image:
            c["image"] = args.image
    if args.image and "@sha256:" in args.image:
        old_digest = pod["metadata"]["annotations"].get("fs2.nebius/runtime-image-digest", "")
        new_digest = "sha256:" + args.image.split("@sha256:", 1)[1]
        pod["metadata"]["annotations"]["fs2.nebius/runtime-image-digest"] = new_digest
        if old_digest and old_digest != new_digest:
            for c in spec.get("initContainers", []) + spec["containers"]:
                for e in c.get("env", []):
                    if "value" in e:
                        e["value"] = e["value"].replace(old_digest.removeprefix("sha256:"), new_digest.removeprefix("sha256:"))
    pod["metadata"]["annotations"]["fs2.nebius/compile-cache-abi"] = "driver-580.159.04-sm90"
    if getattr(args, "cache_cohort", None):
        pod["metadata"]["annotations"]["fs2.nebius/cache-cohort"] = args.cache_cohort
    if args.action in ("hold-cache", "release-cache"):
        if args.model != "evo2-40b" or not args.node:
            raise ValueError("Cache mount holder requires explicit Evo2 node")
        name = "fs2-mm-evo2-cache-holder-20260907"
        pod["metadata"]["name"] = name
        pod["metadata"]["annotations"]["fs2.nebius/cache-cohort"] = "read-only-mount-holder-no-prewarming"
        spec.pop("initContainers", None)
        spec["activeDeadlineSeconds"] = 7200
        spec["containers"] = [{"name": "holder", "image": REGISTRY + "/vllm-openai@sha256:2286e8533ca8b6bc777594bae30524f1426ba46ca21797524e06df6a94b06635",
            "command": ["python3", "-c", "import time; print('read-only PVC mount held; no model file read', flush=True); time.sleep(7200)"],
            "resources": {"requests": {"cpu": "50m", "memory": "64Mi"}, "limits": {"cpu": "100m", "memory": "128Mi"}},
            "volumeMounts": [{"name": "model-cache", "mountPath": "/model-cache", "readOnly": True}]}]
        spec["volumes"] = [{"name": "model-cache", "persistentVolumeClaim": {"claimName": cache, "readOnly": True}}]
        pod["metadata"]["annotations"]["fs2.nebius/runtime-image-digest"] = "sha256:2286e8533ca8b6bc777594bae30524f1426ba46ca21797524e06df6a94b06635"
    elif args.action == "stage":
        if args.model != "evo2-40b":
            raise ValueError("Separate staging is only required for Evo2")
        name = "fs2-mm-evo2-stage-20260907"
        pod["metadata"]["name"] = name
        stage = copy.deepcopy(next(c for c in spec["initContainers"] if c["name"] == "materialize-checkpoint"))
        # This existing H100 image has huggingface_hub and avoids waiting for
        # the GPU runtime image; the exact pinned staging source is unchanged.
        stage["image"] = REGISTRY + "/vllm-openai@sha256:2286e8533ca8b6bc777594bae30524f1426ba46ca21797524e06df6a94b06635"
        stage["command"] = ["python3", "-c", (ROOT / "models/general-media/evo2_stage.py").read_text()]
        spec["containers"] = [stage]
        spec.pop("initContainers", None)
    elif args.action == "preflight":
        if args.model != "evo2-40b" or not args.image or "@sha256:" not in args.image:
            raise ValueError("Evo2 preflight requires an immutable H100 successor image")
        name = "fs2-mm-evo2-preflight-20260907"
        pod["metadata"]["name"] = name
        main = next(c for c in spec["containers"] if c["name"] == "model")
        main["command"] = ["python3", "-c", Path(__file__).with_name("evo2_preflight.py").read_text()]
        main.pop("args", None)
        main["env"] = [{"name": "TRITON_CACHE_DIR", "value": "/tmp/triton"}]
        main["resources"] = {"requests": {"cpu": "2", "memory": "8Gi", "nvidia.com/gpu": "2"}, "limits": {"cpu": "8", "memory": "16Gi", "nvidia.com/gpu": "2"}}
        main.pop("volumeMounts", None)
        spec["containers"] = [main]
        spec.pop("initContainers", None)
        spec.pop("volumes", None)
    write(args.output / "pod-manifest.json", pod)
    write(args.output / "pvc-manifest.json", pvc)
    if args.action in ("create", "stage", "preflight", "hold-cache"):
        if args.action != "preflight" and not existing_cache:
            kube("apply", "--dry-run=client", "-f", "-", data=json.dumps(pvc))
            kube("apply", "-f", "-", data=json.dumps(pvc))
        kube("create", "--dry-run=client", "-f", "-", data=json.dumps(pod))
        receipt = {"probe_requested_at": utc(), "source_manifest_sha256": hashlib.sha256(
            (ROOT / "models/general-media/k8s" / (args.model + ".yaml")).read_bytes()).hexdigest()}
        receipt["create_output"] = kube("create", "-f", "-", data=json.dumps(pod))
        write(args.output / "created.json", receipt)
        print(json.dumps({"pod": name, **receipt}), flush=True)
        return
    current = json.loads(kube("get", "pod", name, "-o", "json"))
    if current["metadata"]["labels"].get("fs2.nebius/task") != TASK:
        raise RuntimeError("ownership mismatch")
    if args.action in ("delete", "release-cache"):
        print(kube("delete", "pod", name, "--grace-period=5", "--wait=true"))
        return
    log_files = {}
    followers = []
    deadline = time.monotonic() + args.timeout
    try:
        while time.monotonic() < deadline:
            current = json.loads(kube("get", "pod", name, "-o", "json"))
            write(args.output / "pod-latest.json", current)
            statuses = current.get("status", {}).get("containerStatuses", []) + current.get("status", {}).get("initContainerStatuses", [])
            for cs in statuses:
                if cs["name"] not in log_files and ("running" in cs["state"] or "terminated" in cs["state"]):
                    f = (args.output / (cs["name"] + ".log")).open("w")
                    log_files[cs["name"]] = f
                    followers.append(subprocess.Popen(k + ["logs", name, "-c", cs["name"], "--timestamps", "-f"], stdout=f, stderr=subprocess.STDOUT))
            if any(cs.get("state", {}).get("terminated", {}).get("exitCode", 0) != 0 for cs in statuses):
                raise RuntimeError("container terminated unsuccessfully; inspect retained logs")
            for log_path in args.output.glob("*.log"):
                if "evo2-lean-server: LOAD_FAIL" in log_path.read_text(errors="replace"):
                    raise RuntimeError("Evo2 reported model initialization failure; inspect retained logs")
            if any(c["type"] == "Ready" and c["status"] == "True" for c in current.get("status", {}).get("conditions", [])):
                break
            time.sleep(5)
        else:
            raise TimeoutError("model readiness timeout")
        write(args.output / "pod-ready.json", current)
        node = json.loads(kube("get", "node", current["spec"]["nodeName"], "-o", "json"))
        write(args.output / "node.json", node)
        events = json.loads(kube("get", "events", "--field-selector", f"involvedObject.uid={current['metadata']['uid']}", "-o", "json"))
        write(args.output / "events.json", events)
        container = spec["containers"][0]["name"]
        gpu = kube("exec", name, "-c", container, "--", "nvidia-smi", "--query-gpu=name,uuid,driver_version,memory.total,compute_cap", "--format=csv,noheader")
        (args.output / "gpu.txt").write_text(gpu)
        if args.model == "evo2-40b":
            (args.output / "process-io-at-ready.txt").write_text(kube("exec", name, "-c", "model", "--", "head", "-8", "/proc/1/io"))
        print(json.dumps({"pod": name, "ready_observed_at": utc(), "node": current["spec"]["nodeName"], "gpu": gpu}), flush=True)
        if args.action == "observe":
            return
        pf_log = (args.output / "port-forward.log").open("w")
        pf = subprocess.Popen(k + ["port-forward", "pod/" + name, f"{args.port}:8000"], stdout=pf_log, stderr=subprocess.STDOUT)
        try:
            time.sleep(2)
            base = f"http://127.0.0.1:{args.port}"
            contract_path = ROOT / "catalog/runtime/validators/assets" / (args.model + ".json")
            contract = json.loads(contract_path.read_text()) if contract_path.exists() else None
            if args.model == "evo2-40b":
                with urlopen(base + "/v1/runtime", timeout=30) as response:
                    identity = json.loads(response.read(1024 * 1024))
                write(args.output / "runtime-identity.json", identity)
                validator = Path(__file__).with_name("evo2_hopper_validate.py")
                completed = subprocess.run(["python3", str(validator), "--base-url", base,
                    "--receipt-dir", str(args.output / "evo2-hopper-receipts"),
                    "--run-id", name + "-hopper-a", "--run-id", name + "-hopper-b",
                    "--timeout", "900"], capture_output=True, text=True)
                (args.output / "hopper-validator.log").write_text(completed.stdout + completed.stderr)
                completed.check_returncode()
                summary = json.loads((args.output / "evo2-hopper-receipts/summary.json").read_text())
                requests = [{"request_at": case["started_at"], "elapsed_seconds": case["elapsed_seconds"],
                    "response_sha256": case["response_sha256"], "response_bytes": case["response_bytes"]}
                    for case in summary["cases"]]
                result = {"status": "PASS", "contract": "two-pinned-deterministic-DNA20-sequences",
                    "oracle_profile": summary["profile"], "oracle_sha256": summary["oracle_sha256"],
                    "receipt_dir": "evo2-hopper-receipts", "requests": requests}
            elif args.model == "nv-segment-ct":
                result = module("validate_nv_segment_ct").validate(base, contract)
            else:
                results = []
                for i, item in enumerate(contract["requests"]):
                    payload = item.get("wire_request", item.get("request"))
                    endpoint = "/v1/chat/completions" if args.model == "nv-reason-cxr-3b" else "/generate"
                    before = utc()
                    start = time.monotonic()
                    with urlopen(Request(base + endpoint, data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"}), timeout=900) as response:
                        raw = response.read(48 * 1024 * 1024)
                    path = args.output / f"response-{i}.bin"
                    path.write_bytes(raw)
                    results.append({"request_at": before, "elapsed_seconds": time.monotonic()-start,
                        "response_sha256": hashlib.sha256(raw).hexdigest(), "response_bytes": len(raw)})
                paths = [args.output / f"response-{i}.bin" for i in range(2)]
                if args.model == "sdxl":
                    # Cross-GPU output is not bitwise identical. Retain all model,
                    # dimensions, nonconstant and envelope checks; record hashes.
                    portable = copy.deepcopy(contract)
                    for item in portable["requests"]:
                        item["oracle"].pop("expected_bytes", None)
                        item["oracle"].pop("expected_sha256", None)
                    validation = module("validate_sdxl").validate(portable, paths)
                else:
                    validation = module("validate_response").validate(contract, paths)
                result = {"status": "PASS", "requests": results, "validation": validation}
            write(args.output / "semantic.json", result)
            print(json.dumps(result), flush=True)
        finally:
            pf.terminate()
            pf.wait(timeout=15)
            pf_log.close()
    except BaseException as exc:
        write(args.output / f"failure-{time.time_ns()}.json", {"at": utc(), "type": type(exc).__name__, "message": str(exc)})
        raise
    finally:
        for proc in followers:
            proc.terminate()
        for proc in followers:
            proc.wait(timeout=15)
        for f in log_files.values():
            f.close()
        # Failed readiness trials are evidence too; retain the scheduling,
        # image-pull and node context rather than only saving successful Pods.
        try:
            write(args.output / "events.json", json.loads(kube("get", "events", "--field-selector", f"involvedObject.uid={current['metadata']['uid']}", "-o", "json")))
            if current["spec"].get("nodeName"):
                write(args.output / "node.json", json.loads(kube("get", "node", current["spec"]["nodeName"], "-o", "json")))
        except subprocess.CalledProcessError:
            pass


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--kubeconfig", required=True)
    p.add_argument("--context", default="k8s-inference-h100")
    p.add_argument("--model", required=True, choices=MODELS)
    p.add_argument("--repetition", type=int, default=1)
    p.add_argument("--output", required=True, type=Path)
    p.add_argument("--action", choices=("create", "stage", "preflight", "hold-cache", "release-cache", "observe", "validate", "delete"), required=True)
    p.add_argument("--timeout", type=int, default=3600)
    p.add_argument("--port", type=int, default=29380)
    p.add_argument("--image")
    p.add_argument("--node", help="Optional exact observed node for a retained-image/page-cache cohort")
    p.add_argument("--existing-cache-pvc", help="Consume a release-owned Bound cache without creating or modifying it")
    p.add_argument("--cache-cohort", help="Explicit storage/cache condition recorded on this isolated Pod")
    run(p.parse_args())
