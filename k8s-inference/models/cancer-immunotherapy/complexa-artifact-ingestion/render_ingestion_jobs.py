#!/usr/bin/env python3
"""Render the staging Job that ingests pinned public checkpoints into a claim.

No project, region, registry, cluster, storage class, node pool, or GPU type is
written here. Every one of those arrives as a flag, so the same renderer serves
the proof-of-concept claim and whatever regional store replaces it.

Two properties of the target claim shape the pod and are not negotiable:

- The volume driver is registered only on nodes carrying the storage capability
  label, so staging selects on that label and tolerates no GPU taint. Staging is
  I/O bound and has no business occupying an H100.
- The claim root is setgid and group-writable. The pod therefore *joins* that
  group and never sets ``fsGroup``: Kubernetes applies fsGroup ownership to the
  whole volume rather than to the sub-path a pod mounts, so setting it here
  would recursively rewrite the ownership of the tenant-private AlphaFold 3 and
  PyRosetta trees that share this claim.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

LABEL_PREFIX = "fs2.nebius.ai"
CLAIM_MOUNT = "/claim"
TOOL_MOUNT = "/opt/fs2-ingestion"


def labels(run_id: str, role: str) -> dict[str, str]:
    return {
        f"{LABEL_PREFIX}/component": "complexa-artifact-ingestion",
        f"{LABEL_PREFIX}/role": role,
        f"{LABEL_PREFIX}/run-id": run_id,
    }


def stage_job(
    *,
    name: str,
    namespace: str,
    run_id: str,
    image: str,
    claim: str,
    config_map: str,
    staging_sub_path: str,
    artifact_ids: tuple[str, ...],
    node_selectors: dict[str, str],
    run_as_user: int,
    run_as_group: int,
    supplemental_group: int,
    cpu: str,
    memory: str,
    retries: int,
    deadline_seconds: int,
    continue_on_artifact_error: bool,
) -> dict[str, object]:
    staging_root = f"{CLAIM_MOUNT}/{staging_sub_path}"
    command = [
        "python3",
        f"{TOOL_MOUNT}/fetch_artifacts.py",
        "--contract",
        f"{TOOL_MOUNT}/ingestion-contract.json",
        "--staging-root",
        staging_root,
        "--receipt",
        f"{staging_root}/.receipts/staging.{run_id}.json",
        "--namespace",
        namespace,
        "--claim",
        claim,
        "--sub-path",
        staging_sub_path,
        "--retries",
        str(retries),
    ]
    for artifact_id in artifact_ids:
        command.extend(["--artifact-id", artifact_id])
    if continue_on_artifact_error:
        command.append("--continue-on-artifact-error")

    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {"name": name, "namespace": namespace, "labels": labels(run_id, "stage")},
        "spec": {
            # One pod, resumed by hand if it dies: the tool is resumable, but a
            # second pod writing the same part files concurrently is not safe.
            "backoffLimit": 0,
            "completions": 1,
            "parallelism": 1,
            "activeDeadlineSeconds": deadline_seconds,
            "template": {
                "metadata": {"labels": labels(run_id, "stage")},
                "spec": {
                    "restartPolicy": "Never",
                    "nodeSelector": dict(node_selectors),
                    "securityContext": {
                        "runAsUser": run_as_user,
                        "runAsGroup": run_as_group,
                        "runAsNonRoot": True,
                        "supplementalGroups": [supplemental_group],
                    },
                    "containers": [
                        {
                            "name": "stage",
                            "image": image,
                            "command": command,
                            "env": [{"name": "PYTHONUNBUFFERED", "value": "1"}],
                            "volumeMounts": [
                                {"name": "claim", "mountPath": CLAIM_MOUNT},
                                {"name": "tool", "mountPath": TOOL_MOUNT, "readOnly": True},
                            ],
                            "resources": {
                                "requests": {"cpu": cpu, "memory": memory},
                                "limits": {"cpu": cpu, "memory": memory},
                            },
                            "securityContext": {
                                "allowPrivilegeEscalation": False,
                                "readOnlyRootFilesystem": True,
                                "capabilities": {"drop": ["ALL"]},
                            },
                        }
                    ],
                    "volumes": [
                        {"name": "claim", "persistentVolumeClaim": {"claimName": claim}},
                        {"name": "tool", "configMap": {"name": config_map, "defaultMode": 0o555}},
                    ],
                },
            },
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--name", required=True)
    parser.add_argument("--namespace", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--image", required=True, help="a stdlib-only Python image")
    parser.add_argument("--claim", required=True)
    parser.add_argument("--config-map", required=True)
    parser.add_argument("--staging-sub-path", required=True,
                        help="task-owned private sub-path inside the claim")
    parser.add_argument("--artifact-id", action="append", default=[], dest="artifact_ids")
    parser.add_argument("--node-selector", action="append", default=[], dest="node_selectors",
                        metavar="KEY=VALUE")
    parser.add_argument("--run-as-user", type=int, default=65532)
    parser.add_argument("--run-as-group", type=int, default=65532)
    parser.add_argument("--supplemental-group", type=int, default=65532)
    # Staging is network bound and streams in 8 MiB blocks, so it needs one core
    # for the SHA-256 pass and very little resident memory. Asking for more only
    # makes the pod unschedulable on the storage-capable CPU nodes, which are
    # small and shared with every other staging workload on this claim.
    parser.add_argument("--cpu", default="1")
    parser.add_argument("--memory", default="2Gi")
    parser.add_argument("--retries", type=int, default=8)
    parser.add_argument("--deadline-seconds", type=int, default=10800)
    parser.add_argument("--continue-on-artifact-error", action="store_true")
    options = parser.parse_args(argv)

    selectors: dict[str, str] = {}
    for item in options.node_selectors:
        key, separator, value = item.partition("=")
        if not separator:
            parser.error(f"--node-selector expects KEY=VALUE, got {item!r}")
        selectors[key] = value

    job = stage_job(
        name=options.name,
        namespace=options.namespace,
        run_id=options.run_id,
        image=options.image,
        claim=options.claim,
        config_map=options.config_map,
        staging_sub_path=options.staging_sub_path.strip("/"),
        artifact_ids=tuple(options.artifact_ids),
        node_selectors=selectors,
        run_as_user=options.run_as_user,
        run_as_group=options.run_as_group,
        supplemental_group=options.supplemental_group,
        cpu=options.cpu,
        memory=options.memory,
        retries=options.retries,
        deadline_seconds=options.deadline_seconds,
        continue_on_artifact_error=options.continue_on_artifact_error,
    )
    json.dump(job, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
