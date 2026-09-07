#!/usr/bin/env python3
"""Validate retained original inputs on one isolated ready scientific worker."""

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("pod", "python", "worker-variable"):
        parser.add_argument("--" + name, required=True)
    for name in ("directory", "kubeconfig"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--case", action="append", type=Path, required=True)
    args = parser.parse_args()
    os.umask(0o077)
    args.directory.mkdir(parents=True, exist_ok=True)
    kube = ["kubectl", "--kubeconfig", str(args.kubeconfig), "--context", "k8s-inference-h100", "-n", "fs2-models"]
    health = "import urllib.request;print(urllib.request.urlopen('http://127.0.0.1:8000/health',timeout=2).read().decode())"
    deadline = time.monotonic() + 600
    while time.monotonic() < deadline:
        ready = subprocess.run([*kube, "exec", args.pod, "-c", "scientific-stage", "--", args.python, "-c", health],
                               capture_output=True, text=True)
        if ready.returncode == 0:
            break
        time.sleep(2)
    else:
        raise TimeoutError("worker did not become ready")
    receipt = {"ready": json.loads(ready.stdout), "cases": [], "status": "running"}
    for case in args.case:
        subprocess.run([*kube, "exec", "-i", args.pod, "-c", "scientific-stage", "--", "tar", "-C",
                        "/mnt/fs2-scientific", "-xpf", "-"], input=(case / "prepared-workspace.tar").read_bytes(), check=True)
        original = json.loads((case / "original-command.json").read_bytes())
        flag = "--output-dir" if "--output-dir" in original else "--output"
        output = original[original.index(flag) + 1]
        result = subprocess.run([sys.executable, str(Path(__file__).with_name("validate_scientific.py")),
            "--kubeconfig", str(args.kubeconfig), "--pod", args.pod, "--python", args.python,
            "--command", str(case / "original-command.json"), "--worker-variable", args.worker_variable,
            "--output", output], capture_output=True, text=True, timeout=900)
        (args.directory / (case.name + ".log")).write_text(result.stdout + result.stderr)
        if result.returncode:
            raise RuntimeError("original scientific wrapper failed; full log retained")
        row = json.loads(next(line.removeprefix("FS2_SNAPSHOT_SEMANTIC ") for line in result.stdout.splitlines()
                              if line.startswith("FS2_SNAPSHOT_SEMANTIC ")))
        receipt["cases"].append(row)
        archive = subprocess.check_output([*kube, "exec", args.pod, "-c", "scientific-stage", "--", "tar",
                                           "-C", output, "-cf", "-", "."])
        (args.directory / (case.name + "-outputs.tar")).write_bytes(archive)
        (args.directory / "cases.json").write_text(json.dumps(receipt, indent=2))
        print(json.dumps({"case": case.name, "status": row["status"], "seconds": row["execution_seconds"]}), flush=True)
    receipt["status"] = "passed"
    (args.directory / "cases.json").write_text(json.dumps(receipt, indent=2))


if __name__ == "__main__":
    main()
