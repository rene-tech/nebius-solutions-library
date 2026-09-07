#!/usr/bin/env python3
"""Exercise the production snapshot transform in an isolated labelled Pod."""

import argparse
import copy
import json
from pathlib import Path
import sys

sys.path.insert(
    0, str(Path(__file__).resolve().parents[3] / "components/control-plane/src")
)
from fs2_serve.serving_snapshot import ServingSnapshotBundle, configure_serving_snapshot


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--name", required=True)
    parser.add_argument("--container", required=True)
    parser.add_argument("--test-filesystem-fallback", action="store_true")
    args = parser.parse_args()
    source = json.loads(args.source.read_bytes())
    config = ServingSnapshotBundle.model_validate_json(args.bundle.read_bytes())
    spec = copy.deepcopy(source["spec"]["template"]["spec"])
    configure_serving_snapshot(
        spec,
        config=config,
        runtime_container_name=args.container,
        fallback="normal-load",
    )
    spec["restartPolicy"] = "Never"
    spec["automountServiceAccountToken"] = False
    if args.test_filesystem_fallback:
        command = next(
            item for item in spec["containers"] if item["name"] == args.container
        )["command"]
        command[command.index("--source-directory") + 1] = (
            "/nonexistent-task-only-snapshot"
        )
    print(
        json.dumps(
            {
                "apiVersion": "v1",
                "kind": "Pod",
                "metadata": {
                    "name": args.name,
                    "namespace": source["metadata"]["namespace"],
                    "labels": {
                        "fs2.nebius.ai/test-only": "true",
                        "snapshot.fs2.nebius/task": "fs2-h100-fleet-snapshot-options-r20260907",
                    },
                },
                "spec": spec,
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
