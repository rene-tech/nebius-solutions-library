#!/usr/bin/env python3
"""Value-suppressed final deployment/backfill identity; no Secret reads."""

import argparse
import hashlib
import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path


def digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def main(args):
    inventory = json.loads(args.inventory.read_bytes())
    verified = {row["bucket"]: row for row in inventory["verification"]}
    buckets = []
    for bucket in inventory["buckets"]:
        assert bucket["examples"]["state"] == "complete"
        if not bucket["tenant_id"].startswith("fs2-starter-"):
            assert verified[bucket["bucket"]]["state"] == "passed"
        buckets.append(
            {
                "bucket_identity_sha256": hashlib.sha256(
                    bucket["bucket"].encode()
                ).hexdigest(),
                "examples": bucket["examples"],
                "independent_download": verified.get(bucket["bucket"], {}).get(
                    "state", "separate-canary-receipt"
                ),
            }
        )
    command = [
        "kubectl",
        "--kubeconfig",
        args.kubeconfig,
        "--context",
        args.context,
        "-n",
        "fs2-system",
        "get",
        "deploy",
        "fs2-serve-control-plane",
        "fs2-serve-control-plane-model-controller",
        "fs2-serve-control-plane-admin-console",
        "-o",
        "json",
    ]
    deployments = json.loads(subprocess.check_output(command))["items"]
    workloads = []
    for deployment in deployments:
        assert deployment["spec"]["replicas"] == deployment["status"].get(
            "readyReplicas"
        )
        workloads.append(
            {
                "name": deployment["metadata"]["name"],
                "ready": deployment["status"]["readyReplicas"],
                "image": deployment["spec"]["template"]["spec"]["containers"][0][
                    "image"
                ],
                "pod_template_sha256": digest(deployment["spec"]["template"]),
            }
        )
    helm = [
        "helm",
        "--kubeconfig",
        args.kubeconfig,
        "--kube-context",
        args.context,
        "-n",
        "fs2-system",
    ]
    release = json.loads(
        subprocess.check_output(
            helm + ["history", "fs2-serve-control-plane", "-o", "json"]
        )
    )[-1]
    assert release["status"] == "deployed"
    values = json.loads(
        subprocess.check_output(
            helm + ["get", "values", "fs2-serve-control-plane", "--all", "-o", "json"]
        )
    )
    assert (
        values["customerStorage"]["starterPack"]["enabled"]
        and not values["customerStorage"]["starterPack"]["tenants"]
    )
    report = {
        "at": datetime.now(UTC).isoformat(),
        "cluster_id": "mk8scluster-e00j5z9te7x5dd9g6a",
        "project_id": "project-e00rene",
        "public_endpoint": "https://89.169.99.188",
        "helm_revision": release["revision"],
        "workloads": workloads,
        "live_values_sha256": digest(values),
        "starter_pack": values["customerStorage"]["starterPack"],
        "active_users_at_backfill": len(inventory["users"]),
        "disabled_storage_users_unchanged": sum(
            u["state"] == "disabled" for u in inventory["users"]
        ),
        "completed_buckets": buckets,
        "independently_verified_non_task_buckets": len(verified),
        "source_inventory_sha256": hashlib.sha256(
            args.inventory.read_bytes()
        ).hexdigest(),
    }
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(
        json.dumps(
            {
                "helm_revision": release["revision"],
                "ready_workloads": len(workloads),
                "complete_buckets": len(buckets),
                "verified_non_task_buckets": len(verified),
            }
        )
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kubeconfig", required=True)
    parser.add_argument("--context", required=True)
    parser.add_argument("--inventory", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    main(parser.parse_args())
