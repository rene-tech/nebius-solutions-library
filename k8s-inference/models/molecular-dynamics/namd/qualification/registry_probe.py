"""Read the authorized NGC repository through a bounded task-owned CPU Pod.

The existing pull secret is mounted read-only; credentials never leave the Pod.
Output contains public registry metadata only. Every resource is task-labeled.
"""

import argparse
import json
import subprocess
import time
from pathlib import Path

KUBE = ["kubectl", "--kubeconfig", "/home/tux/secure-handoff/cosmos-stockholm-sandbox2-20260917.kubeconfig",
        "--context", "fs2-remediation-sandbox2", "-n", "fs2-models"]
CRANE = "gcr.io/go-containerregistry/crane@sha256:e78770b31258a3846f878036d9c1f63fbe4c871f9f56990bf77fd95c013e3c1b"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--name", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("args", nargs="+")
    args = parser.parse_args()
    if not args.name.startswith("fs2-namd-r20260923-"):
        raise ValueError("probe name must be task-owned")
    if args.args[0] not in {"ls", "digest", "manifest", "config"}:
        raise ValueError("only read-only registry commands allowed")
    pod = {"apiVersion": "v1", "kind": "Pod", "metadata": {"name": args.name,
           "namespace": "fs2-models", "labels": {"scientific-ai.nebius.com/task": "namd-r20260923"}},
           "spec": {"restartPolicy": "Never", "activeDeadlineSeconds": 300,
                    "automountServiceAccountToken": False,
                    "containers": [{"name": "probe", "image": CRANE, "command": ["crane"],
                                    "args": args.args, "env": [{"name": "DOCKER_CONFIG", "value": "/auth"}],
                                    "volumeMounts": [{"name": "auth", "mountPath": "/auth", "readOnly": True}],
                                    "resources": {"requests": {"cpu": "100m", "memory": "128Mi"},
                                                  "limits": {"cpu": "1", "memory": "1Gi"}}}],
                    "volumes": [{"name": "auth", "secret": {"secretName": "wan2-ngc-pull", "items": [
                        {"key": ".dockerconfigjson", "path": "config.json"}]}}]}}
    payload = json.dumps(pod).encode()
    subprocess.run(KUBE + ["create", "--dry-run=client", "-f", "-"], input=payload, check=True)
    subprocess.run(KUBE + ["create", "-f", "-"], input=payload, check=True)
    start = time.monotonic()
    while time.monotonic() - start < 320:
        receipt = json.loads(subprocess.check_output(KUBE + ["get", "pod", args.name, "-o", "json"]))
        if receipt["status"]["phase"] in {"Succeeded", "Failed"}:
            break
        time.sleep(2)
    logs = subprocess.run(KUBE + ["logs", args.name], capture_output=True, text=True)
    value = {"command": args.args, "pod": args.name, "uid": receipt["metadata"]["uid"],
             "created": receipt["metadata"]["creationTimestamp"], "status": receipt["status"],
             "node": receipt["spec"].get("nodeName"), "output": logs.stdout, "error": logs.stderr}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(value, indent=2) + "\n")
    print(json.dumps(value))


if __name__ == "__main__":
    main()
