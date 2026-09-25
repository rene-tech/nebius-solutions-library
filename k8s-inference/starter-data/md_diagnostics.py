#!/usr/bin/env python3
"""Retain task-owned failed-job logs after controller garbage collection."""

import argparse
import json
import os
import re
import subprocess
from pathlib import Path

QUERY_SCRIPT = """
import json, os, sys, urllib.parse, urllib.request
args = json.loads(sys.argv[1])
url = os.environ["FS2_ADMIN_LOKI_URL"].rstrip("/") + "/loki/api/v1/query_range?" + urllib.parse.urlencode(args)
with urllib.request.urlopen(url, timeout=45) as response:
    print(response.read().decode())
"""


def main(args):
    os.umask(0o077)
    if not re.fullmatch(r"fs2-workflow-[a-z0-9-]+", args.job):
        raise ValueError("explicit_scientific_job_required")
    parameters = {
        "query": '{k8s_namespace_name="fs2-models",k8s_pod_name=~"'
        + args.job
        + '-.*"}',
        "start": args.start,
        "end": args.end,
        "limit": "1000",
        "direction": "forward",
    }
    raw = subprocess.check_output(
        [
            "kubectl",
            "--kubeconfig",
            args.kubeconfig,
            "--context",
            args.context,
            "--request-timeout=60s",
            "-n",
            "fs2-system",
            "exec",
            "deployment/fs2-serve-control-plane",
            "-c",
            "control-plane",
            "--",
            "python",
            "-c",
            QUERY_SCRIPT,
            json.dumps(parameters),
        ]
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("xb") as stream:
        stream.write(raw)
    value = json.loads(raw)
    for series in value["data"]["result"]:
        labels = series["stream"]
        print(
            json.dumps(
                {
                    "container": labels.get("k8s_container_name"),
                    "pod": labels.get("k8s_pod_name"),
                    "lines": len(series["values"]),
                }
            )
        )
        for timestamp, line in series["values"][-50:]:
            # Full payloads stay private; do not expose transport credentials.
            line = re.sub(r"https?://\S+", "[URL redacted]", line)
            line = re.sub(
                r"(?i)(bearer|secret|token|password|access_key)[=: ]+\S+",
                r"\1 [redacted]",
                line,
            )
            print(timestamp, line[:1600])


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("kubeconfig", "context", "job", "start", "end"):
        parser.add_argument("--" + name, required=True)
    parser.add_argument("--output", type=Path, required=True)
    main(parser.parse_args())
