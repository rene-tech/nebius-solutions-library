"""Read-only bounded speech replica/Pod evidence during overlapping public calls."""

import argparse
import json
import subprocess
import time
from datetime import UTC, datetime


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kubeconfig", required=True)
    parser.add_argument("--context", required=True)
    parser.add_argument("--seconds", type=int, default=600)
    args = parser.parse_args()
    if not 1 <= args.seconds <= 1200:
        raise ValueError("observation must remain bounded")
    command = ["kubectl", "--kubeconfig", args.kubeconfig, "--context", args.context, "-n", "fs2-models"]
    names = {"nemotron-speech-en-0-6b", "nemotron-speech-multilingual-0-6b"}
    started = time.monotonic()
    while time.monotonic()-started < args.seconds:
        row = {"at": datetime.now(UTC).isoformat(), "deployments": [], "pods": []}
        for kind in ("deployments", "pods"):
            data = json.loads(subprocess.check_output(command + ["get", kind, "-o", "json"], timeout=30))
            for item in data["items"]:
                label = item["metadata"].get("labels", {}).get("fs2-serve.nebius.ai/model-deployment")
                if label not in names:
                    continue
                metadata, spec, status = item["metadata"], item["spec"], item.get("status", {})
                summary = {"name": metadata["name"], "uid": metadata["uid"], "model": label}
                if kind == "deployments":
                    summary.update(desired=spec.get("replicas", 0), ready=status.get("readyReplicas", 0))
                else:
                    summary.update(node=spec.get("nodeName"), phase=status.get("phase"),
                        ready=any(c["type"] == "Ready" and c["status"] == "True" for c in status.get("conditions", [])),
                        deleting=metadata.get("deletionTimestamp"),
                        conditions=[{"type": c["type"], "status": c["status"], "reason": c.get("reason")}
                                    for c in status.get("conditions", [])])
                row[kind].append(summary)
        print(json.dumps(row, sort_keys=True), flush=True)
        time.sleep(min(10, max(0, args.seconds-(time.monotonic()-started))))


if __name__ == "__main__":
    main()
