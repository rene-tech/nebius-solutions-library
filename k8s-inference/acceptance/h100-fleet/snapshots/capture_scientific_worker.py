#!/usr/bin/env python3
"""Capture an already ready isolated model-only donor and await durable bytes."""

import argparse
import json
import os
from pathlib import Path
import subprocess
import time


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for key in ("pod", "python", "bundle-path"):
        parser.add_argument("--" + key, required=True)
    for key in ("directory", "kubeconfig"):
        parser.add_argument("--" + key, type=Path, required=True)
    args = parser.parse_args()
    os.umask(0o077)
    args.directory.mkdir(parents=True, exist_ok=True)
    kube = ["kubectl", "--kubeconfig", str(args.kubeconfig), "--context", "k8s-inference-h100",
            "-n", "fs2-models"]
    prefix = [*kube, "exec", args.pod, "-c", "scientific-stage", "--", args.python]
    health = "import urllib.request;print(urllib.request.urlopen('http://127.0.0.1:8000/health',timeout=2).read().decode())"
    deadline = time.monotonic() + 600
    while time.monotonic() < deadline:
        ready = subprocess.run([*prefix, "-c", health], capture_output=True, text=True, timeout=30)
        if ready.returncode == 0:
            (args.directory / "ready.json").write_text(ready.stdout)
            print(json.dumps({"event": "donor_model_ready", "pod": args.pod, "ready": json.loads(ready.stdout)}), flush=True)
            break
        time.sleep(2)
    else:
        raise TimeoutError("model worker did not become ready; donor retained for diagnosis")
    prepare = "import urllib.request;print(urllib.request.urlopen(urllib.request.Request('http://127.0.0.1:8000/prepare-snapshot',data=b'{}',headers={'Content-Type':'application/json'})).read().decode())"
    prepared = subprocess.check_output([*prefix, "-c", prepare], text=True)
    (args.directory / "prepare-snapshot.json").write_text(prepared)
    command = (
        "import pathlib,subprocess,sys;root=pathlib.Path(sys.argv[1]);"
        "pid=(root/'live-worker-pid').read_text().strip();"
        "raise SystemExit(subprocess.call([sys.executable,'/snapshot-source/process_checkpoint.py',"
        "'capture','--pid',pid,'--directory',str(root/'images')]))"
    )
    with (args.directory / "capture.log").open("w") as log:
        result = subprocess.run([*prefix, "-c", command, "/checkpoints/" + args.bundle_path], stdout=log,
                                stderr=subprocess.STDOUT, timeout=1800)
    if result.returncode:
        raise RuntimeError("capture failed; exact log retained and donor left for diagnosis")
    pod = subprocess.check_output([*kube, "get", "pod", args.pod, "-o", "json"])
    (args.directory / "captured-pod-private.json").write_bytes(pod)
    log = subprocess.check_output([*kube, "exec", args.pod, "-c", "scientific-stage", "--", "tail", "-n", "100",
                                   "/checkpoints/" + args.bundle_path + "/worker.log"])
    (args.directory / "captured-worker.log").write_bytes(log)
    subprocess.run([*kube, "delete", "pod", args.pod, "--wait=true", "--timeout=90s"], check=True)
    print(json.dumps({"status": "captured-durable-donor-deleted", "pod": args.pod}))


if __name__ == "__main__":
    main()
