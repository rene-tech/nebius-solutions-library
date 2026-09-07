#!/usr/bin/env python3
"""Read-only exact production runtime handoff with the original Evo2 fixtures."""
import argparse
import datetime as dt
import json
from pathlib import Path
import subprocess
import sys
import time


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kubeconfig", required=True)
    parser.add_argument("--pod", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    kube = ["kubectl", "--kubeconfig", args.kubeconfig, "--context", "k8s-inference-h100", "-n", "fs2-models"]
    raw = subprocess.check_output([*kube, "get", "pod", args.pod, "-o", "json"])
    (args.output / "pod.json").write_bytes(raw)
    pod = json.loads(raw)
    expected = json.loads(Path(__file__).with_name("integration.json").read_text())["models"]["evo2-40b"]
    assert pod["spec"]["containers"][0]["image"] == expected["image"]
    claims = {v["persistentVolumeClaim"]["claimName"] for v in pod["spec"]["volumes"] if "persistentVolumeClaim" in v}
    assert expected["cache_pvc"] in claims
    assert next(c for c in pod["status"]["conditions"] if c["type"] == "Ready")["status"] == "True"
    port = 19746
    with (args.output / "port-forward.log").open("wb") as log:
        forward = subprocess.Popen([*kube, "port-forward", "pod/" + args.pod, f"{port}:8000"], stdout=log, stderr=log)
        try:
            time.sleep(2)
            command = [sys.executable, str(Path(__file__).with_name("evo2_hopper_validate.py")),
                       "--base-url", f"http://127.0.0.1:{port}", "--receipt-dir", str(args.output / "semantics"),
                       "--run-id", "production-evo2-a", "--run-id", "production-evo2-b"]
            result = subprocess.run(command, capture_output=True, text=True, timeout=600)
            (args.output / "validator.stdout").write_text(result.stdout)
            (args.output / "validator.stderr").write_text(result.stderr)
            result.check_returncode()
        finally:
            forward.terminate()
            forward.wait(timeout=10)
    receipt = {"status": "PASS", "scope": "direct-production-Pod-not-public-HTTP-or-MCP", "pod": args.pod,
               "pod_uid": pod["metadata"]["uid"], "image": expected["image"], "cache_pvc": expected["cache_pvc"],
               "model_revision": expected["model_revision"], "completed_at": dt.datetime.now(dt.timezone.utc).isoformat(),
               "semantics": json.loads(result.stdout)}
    (args.output / "receipt.json").write_text(json.dumps(receipt, indent=2) + "\n")
    print(json.dumps(receipt), flush=True)


if __name__ == "__main__":
    main()
