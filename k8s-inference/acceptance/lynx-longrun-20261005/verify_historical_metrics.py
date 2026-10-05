"""Verify fresh live queues and recurrent historical accounting on every reader.

Read-only, bounded, credential-free localhost scrapes through the existing
operator Kubernetes path. Persist only counts, freshness and Pod/image identity;
never retain metric labels or request/customer contents. This is not an SLO.
"""

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import subprocess
import time

from observe_database_load import CONTEXT

SELECTOR = "app.kubernetes.io/component=gateway,app.kubernetes.io/instance=fs2-serve-control-plane"
PROGRAM = """
import json,time,urllib.request
started=time.monotonic()
with urllib.request.urlopen('http://127.0.0.1:8080/metrics',timeout=5) as response:
    body=response.read().decode()
values={}
counts={'queues':0,'queue_ages':0,'history':0}
for line in body.splitlines():
    if line.startswith('fs2_serve_historical_accounting_'):
        name,value=line.split()
        values[name.removeprefix('fs2_serve_historical_accounting_')]=float(value)
    elif line.startswith('fs2_serve_operations{'):
        counts['queues']+=1
    elif line.startswith('fs2_serve_oldest_queued_operation_age_seconds{'):
        counts['queue_ages']+=1
    elif line.startswith(('fs2_serve_requests_total{','fs2_serve_public_exchanges_total{',
                          'fs2_serve_lifecycle_gpu_seconds_total{')):
        counts['history']+=1
print(json.dumps({'duration_seconds':time.monotonic()-started,'freshness':values,'series_counts':counts}))
"""


def judge(samples, names):
    issues = []
    for name in names:
        rows = [
            row for sample in samples for row in sample["readers"] if row["pod"] == name
        ]
        if len(rows) != len(samples) or not rows:
            issues.append(name + ": missing observation")
        if any(row.get("status") != "observed" for row in rows):
            issues.append(name + ": scrape failure")
        good = [row for row in rows if row.get("status") == "observed"]
        if any(row["series_counts"]["queues"] == 0 for row in good):
            issues.append(name + ": live queue series missing")
        fresh = [row for row in good if row["freshness"].get("available") == 1]
        if (
            len({row["freshness"].get("sampled_timestamp_seconds") for row in fresh})
            < 2
        ):
            issues.append(name + ": recurrent completed history refresh not observed")
        if any(not 0 <= row["freshness"].get("age_seconds", 31) < 30 for row in fresh):
            issues.append(name + ": stale history incorrectly available")
        if any(row["series_counts"]["history"] == 0 for row in fresh):
            issues.append(name + ": completed historical sample has no history series")
        if any(
            row["series_counts"]["history"] != 0
            for row in good
            if row["freshness"].get("available") == 0
        ):
            issues.append(name + ": unavailable history still exported")
    return {"status": "failed" if issues else "passed", "issues": issues}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if "@sha256:" not in args.image:
        parser.error("Select an immutable image")
    os.umask(0o077)
    args.output.mkdir(parents=True, exist_ok=False)
    base = [
        "kubectl",
        "--context",
        CONTEXT,
        "--request-timeout=10s",
        "-n",
        "fs2-system",
    ]
    deployment = json.loads(
        subprocess.check_output(
            [*base, "get", "deployment", "fs2-serve-control-plane", "-o", "json"],
            timeout=20,
        )
    )
    pods = json.loads(
        subprocess.check_output(
            [*base, "get", "pods", "-l", SELECTOR, "-o", "json"],
            timeout=20,
        )
    )["items"]
    identities = []
    for pod in pods:
        if pod["metadata"].get("deletionTimestamp"):
            raise ValueError("Old or terminating readers remain")
        container = next(
            item
            for item in pod["spec"]["containers"]
            if item["name"] == "control-plane"
        )
        if container["image"] != args.image or not any(
            item["type"] == "Ready" and item["status"] == "True"
            for item in pod["status"].get("conditions", [])
        ):
            raise ValueError("Unexpected reader image or readiness")
        identities.append(
            {
                "pod": pod["metadata"]["name"],
                "uid": pod["metadata"]["uid"],
                "image": args.image,
            }
        )
    if not identities or len(identities) != deployment["spec"]["replicas"]:
        raise ValueError(
            "The observed reader count differs from the current desired fleet"
        )

    def scrape(identity):
        row = {"pod": identity["pod"], "status": "unavailable"}
        try:
            result = subprocess.run(
                [
                    *base,
                    "exec",
                    identity["pod"],
                    "-c",
                    "control-plane",
                    "--",
                    "python",
                    "-c",
                    PROGRAM,
                ],
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
            if result.returncode == 0:
                row.update(json.loads(result.stdout), status="observed")
            else:
                row["exit_code"] = result.returncode
        except (subprocess.TimeoutExpired, ValueError) as error:
            row["error_type"] = type(error).__name__
        return row

    samples = []
    with ThreadPoolExecutor(max_workers=3) as pool:
        for index in range(10):
            tick = time.monotonic()
            sample = {
                "at": datetime.now(UTC).isoformat(),
                "readers": list(pool.map(scrape, identities)),
            }
            samples.append(sample)
            (args.output / "observations.json").write_text(
                json.dumps({"identities": identities, "samples": samples}, indent=2)
                + "\n"
            )
            print(
                json.dumps({"sample": index + 1, "readers": len(sample["readers"])}),
                flush=True,
            )
            if index != 9:
                time.sleep(max(0, 5 - (time.monotonic() - tick)))
    verdict = judge(samples, [identity["pod"] for identity in identities])
    (args.output / "verification.json").write_text(json.dumps(verdict, indent=2) + "\n")
    print(json.dumps(verdict))
    if verdict["status"] != "passed":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
