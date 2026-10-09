"""Read-only deployment, replica and startup evidence for the two new Apps."""

import argparse
import json
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path


def main(args):
    samples = []
    end = time.monotonic() + args.seconds
    args.output.parent.mkdir(parents=True, exist_ok=True)
    while time.monotonic() < end:
        row = {"at": datetime.now(UTC).isoformat()}
        for kind in ("deployments", "pods", "modeldeployments"):
            raw = subprocess.check_output(
                [
                    "kubectl",
                    "--context",
                    args.context,
                    "-n",
                    "fs2-models",
                    "get",
                    kind,
                    "-o",
                    "json",
                ],
                text=True,
            )
            selected = []
            for resource in json.loads(raw)["items"]:
                name = resource["metadata"]["name"]
                if not any(
                    name == model or name.startswith(model + "-")
                    for model in ("admet-ai", "ctoxpred2")
                ):
                    continue
                selected.append(
                    {
                        "name": name,
                        "uid": resource["metadata"]["uid"],
                        "generation": resource["metadata"].get("generation"),
                        "created_at": resource["metadata"].get("creationTimestamp"),
                        "spec": resource.get("spec"),
                        "status": resource.get("status") or {},
                    }
                )
            row[kind] = selected
        samples.append(row)
        args.output.write_text(json.dumps(samples, indent=2) + "\n")
        print(
            json.dumps(
                {
                    "at": row["at"],
                    "models": [
                        {
                            "name": item["name"],
                            "phase": item["status"].get("phase"),
                            "replicas": item["status"].get("replicas"),
                        }
                        for item in row["modeldeployments"]
                    ],
                }
            ),
            flush=True,
        )
        time.sleep(args.interval)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--context", default="nebius-mk8s-k8s-inference-h100-e00j5z9te7x5dd9g6a"
    )
    parser.add_argument("--seconds", type=int, default=1200)
    parser.add_argument("--interval", type=int, default=10)
    parser.add_argument("--output", type=Path, required=True)
    main(parser.parse_args())
