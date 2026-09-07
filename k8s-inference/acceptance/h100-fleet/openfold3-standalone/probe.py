#!/usr/bin/env python3
"""Isolated Preview2 fresh-process trials, with original structural oracles."""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import importlib.util
import json
import subprocess
import sys
import time
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[3]
TASK = "fs2-h100-fleet-medical-media-r20260907"
CACHE = "fs2-mm-openfold3-preview2-cache-20260907"
VALIDATOR = ROOT / "catalog/runtime/packaged-repository/nim-fast-start/faststart-v2/openfold3-native/validate_openfold3.py"
FIXTURE = VALIDATOR.parent / "fixtures/request-20aa.json"


def utc():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def write(path, value):
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def manifest(image, repetition, node=None):
    if "@sha256:" not in image:
        raise ValueError("A resolved immutable image is required")
    cache_root = "/model-cache/" + image.split("@sha256:")[1] + "/driver-580.159.04-sm90"
    selector = {"accelerator.fs2.nebius/class": "nvidia-h100-sxm5-80gb", "accelerator.fs2.nebius/pool-id": "h100-reserved-8x"}
    if node:
        selector["kubernetes.io/hostname"] = node
    resources = {"requests": {"cpu": "6", "memory": "32Gi", "nvidia.com/gpu": "1"},
                 "limits": {"cpu": "16", "memory": "64Gi", "nvidia.com/gpu": "1"}}
    return {"apiVersion": "v1", "kind": "Pod", "metadata": {
        "name": f"fs2-mm-openfold3-preview2-r{repetition:02d}-20260907", "namespace": "fs2-models",
        "labels": {"fs2.nebius/task": TASK, "fs2.nebius/probe-model": "openfold3-preview2"},
        "annotations": {"fs2.nebius/cache-cohort": "image-baked-preview2-weights-retained-driver-SM-compile-PVC",
                        "fs2.nebius/model-revision": "4a0eaeaeae8ca1d815d0a97d8eb45d639b91a47e"}},
        "spec": {"restartPolicy": "Never", "activeDeadlineSeconds": 7200,
            "automountServiceAccountToken": False, "enableServiceLinks": False,
            "securityContext": {"runAsNonRoot": True, "runAsUser": 10001, "runAsGroup": 10001,
                                "fsGroup": 10001, "fsGroupChangePolicy": "OnRootMismatch"},
            "nodeSelector": selector,
            "tolerations": [{"key": "dedicated", "operator": "Equal", "value": "fs2-inference", "effect": "NoSchedule"}],
            "volumes": [{"name": "cache", "persistentVolumeClaim": {"claimName": CACHE}},
                        {"name": "shm", "emptyDir": {"medium": "Memory", "sizeLimit": "8Gi"}}],
            "containers": [{"name": "model", "image": image, "imagePullPolicy": "IfNotPresent",
                "env": [{"name": "FS2_RUNTIME_CACHE_ROOT", "value": cache_root}, {"name": "MAX_JOBS", "value": "8"}],
                "resources": resources,
                "volumeMounts": [{"name": "cache", "mountPath": "/model-cache"}, {"name": "shm", "mountPath": "/dev/shm"}],
                "startupProbe": {"httpGet": {"path": "/v1/health/ready", "port": 8000}, "periodSeconds": 2, "failureThreshold": 900},
                "readinessProbe": {"httpGet": {"path": "/v1/health/ready", "port": 8000}, "periodSeconds": 2},
                "livenessProbe": {"httpGet": {"path": "/v1/health/live", "port": 8000}, "periodSeconds": 10, "failureThreshold": 6}}]}}


def validate(base, output, prefix):
    spec = importlib.util.spec_from_file_location("original_openfold3_validator", VALIDATOR)
    validator = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = validator
    spec.loader.exec_module(validator)
    fixture = validator._read_fixture(FIXTURE)
    rows = []
    for suffix in ("a", "b", "varied"):
        case_id = prefix + "-" + suffix
        payload = validator._request_for_case(fixture, case_id)
        if suffix == "varied":
            sequence = "MKWVTFISLLFLFSSAYSRGVFRRDTHKSEIAHRFKDLGE"
            molecule = payload["inputs"][0]["molecules"][0]
            molecule["sequence"] = sequence
            molecule["msa"]["main"]["a3m"]["alignment"] = ">query\n" + sequence
        write(output / (suffix + "-request.json"), payload)
        started, monotonic = utc(), time.monotonic()
        request = Request(base + "/biology/openfold/openfold3/predict", data=json.dumps(payload).encode(),
                          headers={"Content-Type": "application/json"})
        try:
            with urlopen(request, timeout=1800) as response:
                raw = response.read(8 * 1024 * 1024 + 1)
        except HTTPError as exc:
            (output / (suffix + "-error.json")).write_bytes(exc.read())
            raise
        (output / (suffix + "-response.json")).write_bytes(raw)
        row = {"case": suffix, "started_at": started, "completed_at": utc(), "seconds": time.monotonic() - monotonic,
               "validation": validator._validate_response(json.loads(raw), case_id)}
        rows.append(row)
        write(output / "semantic-results.json", {"validator_sha256": hashlib.sha256(VALIDATOR.read_bytes()).hexdigest(),
                                                 "original_fixture_sha256": hashlib.sha256(FIXTURE.read_bytes()).hexdigest(), "cases": rows})
        print(json.dumps(row), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("create", "observe", "validate", "delete"))
    parser.add_argument("--kubeconfig", required=True)
    parser.add_argument("--context", default="k8s-inference-h100")
    parser.add_argument("--image", required=True)
    parser.add_argument("--repetition", required=True, type=int)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--node")
    parser.add_argument("--port", type=int, default=19743)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True, mode=0o700)
    k = ["kubectl", "--kubeconfig", args.kubeconfig, "--context", args.context, "-n", "fs2-models"]

    def kube(*words, data=None):
        return subprocess.run(k + list(words), input=data, capture_output=True, text=True, check=True).stdout

    pod = manifest(args.image, args.repetition, args.node)
    name = pod["metadata"]["name"]
    write(args.output / "pod-manifest.json", pod)
    if args.action == "create":
        existing = json.loads(kube("get", "pvc", CACHE, "--ignore-not-found", "-o", "json") or "null")
        if existing is None:
            pvc = {"apiVersion": "v1", "kind": "PersistentVolumeClaim", "metadata": {"name": CACHE, "namespace": "fs2-models",
                "labels": {"fs2.nebius/task": TASK}}, "spec": {"accessModes": ["ReadWriteMany"],
                "storageClassName": "csi-mounted-fs-path-sc", "resources": {"requests": {"storage": "4Gi"}}}}
            kube("create", "-f", "-", data=json.dumps(pvc))
        elif existing["metadata"].get("labels", {}).get("fs2.nebius/task") != TASK:
            raise ValueError("Cache ownership mismatch")
        kube("create", "--dry-run=client", "-f", "-", data=json.dumps(pod))
        receipt = {"requested_at": utc(), "result": kube("create", "-f", "-", data=json.dumps(pod))}
        write(args.output / "created.json", receipt)
        print(json.dumps(receipt), flush=True)
        return
    current = json.loads(kube("get", "pod", name, "-o", "json"))
    if current["metadata"].get("labels", {}).get("fs2.nebius/task") != TASK:
        raise ValueError("Pod ownership mismatch")
    if args.action == "delete":
        result = kube("delete", "pod", name, "--grace-period=10", "--wait=true")
        write(args.output / "deleted.json", {"pod": name, "uid": current["metadata"]["uid"],
                                           "completed_at": utc(), "result": result})
        print(result, flush=True)
        return
    for _ in range(360):
        current = json.loads(kube("get", "pod", name, "-o", "json"))
        write(args.output / "pod-latest.json", current)
        statuses = current.get("status", {}).get("containerStatuses", [])
        if any("running" in status["state"] or "terminated" in status["state"] for status in statuses):
            logs = kube("logs", name, "--timestamps")
            (args.output / "model.log").write_text(logs)
            if '"phase": "LOAD_FAILED"' in logs:
                raise RuntimeError("Model initialization failed; retained model.log")
        if any(status["type"] == "Ready" and status["status"] == "True" for status in current.get("status", {}).get("conditions", [])):
            break
        if current.get("status", {}).get("phase") == "Failed":
            raise RuntimeError("Pod failed")
        time.sleep(5)
    else:
        raise TimeoutError("Pod readiness deadline exceeded")
    write(args.output / "pod-ready.json", current)
    cache = json.loads(kube("get", "pvc", CACHE, "-o", "json"))
    write(args.output / "cache-pvc.json", cache)
    write(args.output / "cache-pv.json", json.loads(kube("get", "pv", cache["spec"]["volumeName"], "-o", "json")))
    write(args.output / "node.json", json.loads(kube("get", "node", current["spec"]["nodeName"], "-o", "json")))
    write(args.output / "events.json", json.loads(kube("get", "events", "--field-selector", "involvedObject.uid=" + current["metadata"]["uid"], "-o", "json")))
    (args.output / "gpu.txt").write_text(kube("exec", name, "--", "nvidia-smi", "--query-gpu=name,uuid,driver_version,memory.total,compute_cap", "--format=csv,noheader"))
    print(json.dumps({"ready_observed_at": utc(), "pod": name, "node": current["spec"]["nodeName"]}), flush=True)
    if args.action == "observe":
        return
    with (args.output / "port-forward.log").open("w") as log:
        forward = subprocess.Popen(k + ["port-forward", "pod/" + name, f"{args.port}:8000"], stdout=log, stderr=subprocess.STDOUT)
        try:
            time.sleep(2)
            with urlopen(f"http://127.0.0.1:{args.port}/v1/runtime", timeout=30) as response:
                write(args.output / "runtime.json", json.loads(response.read()))
            validate(f"http://127.0.0.1:{args.port}", args.output, name)
        finally:
            forward.terminate()
            forward.wait(timeout=10)
            (args.output / "model.log").write_text(kube("logs", name, "--timestamps"))


if __name__ == "__main__":
    main()
