"""Bounded task-owned Pod qualification; root authorizes each immutable manifest."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path

import yaml

KUBECONFIG = "/home/tux/.local/state/k8s-inference-dual-acceptance/h100/run/kubeconfig"
COLLECTOR = Path("/home/tux/.codex/skills/gpu-performance/scripts/collect_blackwell_env.py")


def now():
    return datetime.now(UTC).isoformat()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    os.umask(0o077)
    manifest = yaml.safe_load(args.manifest.read_text())
    name = manifest["metadata"]["name"]
    model = manifest["metadata"]["labels"]["acceptance.fs2.nebius/model"]
    assert name == f"fs2-aging-20260908-{model}-r01" and manifest["kind"] == "Pod"
    assert manifest["metadata"]["namespace"] == "fs2-models"
    assert "@sha256:" in manifest["spec"]["containers"][0]["image"]
    args.output.mkdir(parents=True, exist_ok=False)
    base = ["kubectl", "--kubeconfig", KUBECONFIG, "--context", "k8s-inference-h100", "--request-timeout=15s", "-n", "fs2-models"]
    ordinal = 0

    def capture(label, command, stdin=None, timeout=45):
        nonlocal ordinal
        ordinal += 1
        started = now()
        reply = subprocess.run(base + command, input=stdin, capture_output=True, text=True, timeout=timeout, check=False)
        receipt = {
            "started_at": started, "completed_at": now(), "command": command,
            "returncode": reply.returncode, "stdout": reply.stdout, "stderr": reply.stderr,
        }
        with (args.output / f"{ordinal:03d}-{label}.json").open("x") as stream:
            json.dump(receipt, stream, indent=2)
        assert reply.returncode == 0, f"{label}: {reply.stderr}"
        return reply.stdout

    capture("dry-run", ["create", "--dry-run=client", "-f", str(args.manifest)])
    created = json.loads(capture("created", ["create", "-f", str(args.manifest), "-o", "json"]))
    uid = created["metadata"]["uid"]
    print(json.dumps({"event": "created", "at": now(), "name": name, "uid": uid}), flush=True)
    deadline = time.monotonic() + 900
    while True:
        pod = json.loads(capture("pod", ["get", "pod", name, "-o", "json"]))
        assert pod["metadata"]["uid"] == uid
        assert pod.get("status", {}).get("phase") not in {"Failed", "Succeeded"}
        if any(item["type"] == "Ready" and item["status"] == "True" for item in pod.get("status", {}).get("conditions", [])):
            break
        assert time.monotonic() < deadline, "readiness deadline exceeded; Pod retained for diagnosis"
        time.sleep(2)
    print(json.dumps({"event": "ready", "at": now(), "name": name, "uid": uid}), flush=True)
    capture("events", ["get", "events", "--field-selector", f"involvedObject.uid={uid}", "-o", "json"])
    capture("startup-logs", ["logs", name, "--timestamps=true"])
    capture("environment", ["exec", "-i", name, "--", "python", "-", "--compact"], stdin=COLLECTOR.read_text(), timeout=180)
    capture("qualification", ["exec", "-i", name, "--", "python", "-", model], stdin=Path(__file__).with_name("in_pod.py").read_text(), timeout=300)
    capture("terminal-logs", ["logs", name, "--timestamps=true"])
    final = json.loads(capture("pre-cleanup", ["get", "pod", name, "-o", "json"]))
    assert final["metadata"]["uid"] == uid
    assert all(item["restartCount"] == 0 for item in final["status"]["containerStatuses"])
    capture("delete-owned-pod", ["delete", "pod", name, "--wait=true", "--timeout=60s"], timeout=75)
    absent = json.loads(capture("cleanup", ["get", "pods", "--field-selector", f"metadata.name={name}", "-o", "json"]))
    assert absent["items"] == []
    print(json.dumps({"outcome": "passed", "name": name, "uid": uid, "cleaned_up_at": now()}), flush=True)


if __name__ == "__main__":
    main()
