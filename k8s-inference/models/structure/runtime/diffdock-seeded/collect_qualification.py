"""Retain completed GPU probe output before deleting its task-owned Pod."""

import argparse
import hashlib
import json
import subprocess
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pod", required=True)
    parser.add_argument("--kubeconfig", required=True)
    parser.add_argument("--context", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    kube = ["kubectl", "--kubeconfig", args.kubeconfig, "--context", args.context, "-n", "fs2-models"]
    pod = json.loads(subprocess.check_output(kube + ["get", "pod", args.pod, "-o", "json"], text=True))
    if pod["metadata"]["labels"].get("fs2.nebius.ai/qualification") != args.pod:
        raise ValueError("Wrong task-owned Pod identity")
    logs = subprocess.check_output(kube + ["logs", args.pod, "-c", "qualifier"], text=True)
    events = subprocess.check_output(
        kube + ["get", "events", "--field-selector", "involvedObject.uid=" + pod["metadata"]["uid"], "-o", "json"],
        text=True,
    )
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "pod.json").write_text(json.dumps(pod, indent=2) + "\n")
    (args.output / "events.json").write_text(events)
    (args.output / "runtime.log").write_text(logs)
    receipt = None
    for line in logs.splitlines():
        if not line.startswith('{"event":'):
            continue
        value = json.loads(line)
        if value["event"] == "result_artifact":
            name = value["filename"]
            if Path(name).name != name or not name.endswith(".json"):
                raise ValueError("Unexpected result artifact name")
            (args.output / name).write_text(json.dumps(value["document"], sort_keys=True, allow_nan=False) + "\n")
        elif value["event"] == "qualification_completed":
            receipt = {key: item for key, item in value.items() if key != "event"}
    if receipt is None:
        raise ValueError("Probe has not produced a complete qualification receipt")
    for run in receipt["runs"]:
        if hashlib.sha256((args.output / run["result_file"]).read_bytes()).hexdigest() != run["result_sha256"]:
            raise ValueError("Captured result differs from runtime digest")
    (args.output / "receipt.json").write_text(json.dumps(receipt, indent=2) + "\n")
    print(
        json.dumps(
            {
                "pod": args.pod,
                "node": pod["spec"]["nodeName"],
                "gpu": receipt["gpu"],
                "passed": receipt["passed"],
                "run_count": len(receipt["runs"]),
                "schema": receipt["schema"],
                "preprocessing": receipt.get("preprocessing", []),
                "pairs": receipt.get("repeated_pairs", []),
                "scope": receipt.get("scope", receipt.get("scientific_accuracy")),
            }
        )
    )


if __name__ == "__main__":
    main()
