"""Capture sanitized probe/admission evidence for an exact internal QA cohort.

Read-only Kubernetes logs; never retain request bodies, headers, tokens, signed
URLs or unrelated customer requests. Missing probe evidence remains unknown.
"""

import argparse
import json
import os
import re
import subprocess
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

SELECTOR = "app.kubernetes.io/component=gateway,app.kubernetes.io/instance=fs2-serve-control-plane"
READY = re.compile(r'"GET /readyz HTTP/[^\"]+" (\d{3}) ')


def select_events(lines, source_operation):
    probes, admissions = [], []
    path = f"/v1/operations/{source_operation}:resume"
    for line in lines.splitlines():
        timestamp = line.partition(" ")[0]
        match = READY.search(line)
        if match:
            probes.append({"at": timestamp, "status": int(match.group(1))})
        if "fs2_serve.access " not in line:
            continue
        try:
            value = json.loads(line.partition("fs2_serve.access ")[2])
        except ValueError:
            continue
        if value.get("method") == "POST" and value.get("path") == path:
            admissions.append(
                {
                    "at": timestamp,
                    **{
                        key: value.get(key)
                        for key in (
                            "status",
                            "duration_ms",
                            "request_id",
                            "response_complete",
                            "disconnected",
                        )
                    },
                }
            )
    return {"probes": probes, "admissions": admissions}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--context", required=True)
    parser.add_argument("--source-operation", type=UUID, required=True)
    parser.add_argument(
        "--since", required=True, help="UTC RFC3339 start of this exact cohort"
    )
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    datetime.fromisoformat(args.since.replace("Z", "+00:00"))
    os.umask(0o077)
    base = [
        "kubectl",
        "--context",
        args.context,
        "--request-timeout=15s",
        "-n",
        "fs2-system",
    ]
    listed = subprocess.run(
        [*base, "get", "pods", "-l", SELECTOR, "-o", "json"],
        capture_output=True,
        text=True,
        check=True,
        timeout=25,
    )

    def collect(pod):
        name = pod["metadata"]["name"]
        logs = subprocess.run(
            [
                *base,
                "logs",
                name,
                "-c",
                "control-plane",
                "--since-time=" + args.since,
                "--timestamps",
                "--tail=-1",
            ],
            capture_output=True,
            text=True,
            check=True,
            timeout=25,
        )
        return {
            "name": name,
            "uid": pod["metadata"]["uid"],
            "created_at": pod["metadata"]["creationTimestamp"],
            "containers": [
                {
                    key: item.get(key)
                    for key in ("name", "imageID", "ready", "restartCount")
                }
                for item in pod["status"].get("containerStatuses", [])
            ],
            **select_events(logs.stdout, args.source_operation),
        }

    with ThreadPoolExecutor(max_workers=3) as pool:
        pods = list(pool.map(collect, json.loads(listed.stdout)["items"]))
    failures = sum(probe["status"] != 200 for pod in pods for probe in pod["probes"])
    result = {
        "observed_at": datetime.now(UTC).isoformat(),
        "since": args.since,
        "source_operation": str(args.source_operation),
        "readiness_status": "unknown"
        if len(pods) != 3 or any(not pod["probes"] for pod in pods)
        else "failed"
        if failures
        else "passed",
        "non_200_probes": failures,
        "pods": pods,
        "scope": "Kubernetes readiness probes and this internal source's resume requests, not an availability SLO",
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    print(
        json.dumps(
            {
                "readiness_status": result["readiness_status"],
                "non_200_probes": failures,
                "pods": len(pods),
            }
        )
    )


if __name__ == "__main__":
    main()
