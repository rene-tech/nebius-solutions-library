"""Recover the exact observed BoltzGen design Pod spec as a private Job template.

The controller already removed the original Job. This does not reconstruct
model configuration: it copies the captured, admitted Pod specification and
retains the source Pod UID/hash. Output contains private workload handles and
must not be committed. The isolated runner replaces only benchmark transport,
observer and object-identity fields, with authorized digest-matched inputs.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path

from capture_startup_logs import consume_json


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--watch", type=Path, required=True)
    parser.add_argument("--pod-uid", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    found = None
    buffer = ""
    with args.watch.open() as stream:
        while chunk := stream.read(1024 * 1024):
            records, buffer = consume_json(buffer + chunk)
            for record in records:
                pod = record.get("object", record)
                if pod.get("metadata", {}).get("uid") == args.pod_uid:
                    found = pod
    if found is None:
        raise ValueError("source Pod UID not found in captured watch")
    labels = found["metadata"].get("labels", {})
    if labels.get("fs2.nebius.ai/model-id") != "boltzgen" or labels.get("fs2.nebius.ai/stage-id") != "design":
        raise ValueError("source is not the BoltzGen design stage")
    source_hash = hashlib.sha256(json.dumps(found["spec"], sort_keys=True).encode()).hexdigest()
    job = {
        "apiVersion": "batch/v1", "kind": "Job",
        "metadata": {
            "name": "observed-boltzgen-design-template",
            "namespace": found["metadata"]["namespace"],
            "labels": labels,
            "annotations": {"benchmark.fs2.nebius/source-pod-uid": args.pod_uid,
                            "benchmark.fs2.nebius/source-pod-spec-sha256": source_hash},
        },
        "spec": {"activeDeadlineSeconds": 7200,
                 "template": {"metadata": {"labels": labels}, "spec": found["spec"]}},
    }
    descriptor = os.open(args.output, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        json.dump(job, stream, indent=2)
        stream.write("\n")
    print(json.dumps({"source_pod_uid": args.pod_uid, "source_spec_sha256": source_hash,
                      "private_template_created": True}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
