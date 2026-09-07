#!/usr/bin/env python3
"""Project matched private trial receipts into a credential-free qualification.

Restored health responses retain the donor's historical model_load_seconds.
That field is deliberately excluded from restore timing: fresh Pod/container
timestamps and observed readiness provide the independent restore clock.
"""

import argparse
from datetime import datetime
import hashlib
import json
from pathlib import Path
from statistics import median


def elapsed(start, end):
    return (
        datetime.fromisoformat(end.replace("Z", "+00:00"))
        - datetime.fromisoformat(start.replace("Z", "+00:00"))
    ).total_seconds()


def summary(values):
    return {
        "n": len(values),
        "median_seconds": median(values),
        "min_seconds": min(values),
        "max_seconds": max(values),
    }


def project(receipt, bundle):
    if receipt["status"] != "passed" or len(receipt["runs"]) != 6:
        raise ValueError("qualification requires all six successful paired trials")
    runs = []
    for source in receipt["runs"]:
        if source["status"] != "passed" or not source["gpu_pod_deleted"]:
            raise ValueError("trial must pass and release its GPU Pod")
        if len(source["cases"]) != 2 or any(
            case["status"] != "passed" for case in source["cases"]
        ):
            raise ValueError("both unchanged scientific wrapper inputs must pass")
        runs.append(
            {
                "repetition": source["repetition"],
                "mode": source["mode"],
                "pod_uid": source["pod_uid"],
                "created_request_at": source["created_request_at"],
                "pod_created_at": source["pod_created_at"],
                "container_started_at": source["container_started_at"],
                "ready_observed_at": source["ready_observed_at"],
                "pod_create_request_to_ready_seconds": source[
                    "creation_to_ready_observed_seconds"
                ],
                "container_start_to_ready_seconds": elapsed(
                    source["container_started_at"], source["ready_observed_at"]
                ),
                "pod_created_to_ready_seconds": elapsed(
                    source["pod_created_at"], source["ready_observed_at"]
                ),
                "container_start_to_first_valid_output_seconds": elapsed(
                    source["container_started_at"], source["cases"][0]["finished_at"]
                ),
                "normal_model_loader_seconds": source["ready"]["model_load_seconds"]
                if source["mode"] == "normal"
                else None,
                "cases": source["cases"],
                "gpu_pod_deleted": True,
            }
        )
    stats = {}
    for mode in ("normal", "restore"):
        selected = [run for run in runs if run["mode"] == mode]
        if len(selected) != 3:
            raise ValueError("three repetitions per mode are required")
        stats[mode] = {
            clock: summary([run[clock] for run in selected])
            for clock in (
                "pod_create_request_to_ready_seconds",
                "container_start_to_ready_seconds",
                "pod_created_to_ready_seconds",
                "container_start_to_first_valid_output_seconds",
            )
        }
    return {
        "schema": "fs2-serve.nebius.ai/scientific-snapshot-qualification/v1",
        "model_id": "protenix-v2",
        "stage_id": "sample-structure",
        "status": "fresh-pod-qualified",
        "production_selectable": False,
        "qualification_scope": "isolated exact-image stage worker; production renderer rollout is separate",
        "date": "2026-09-07",
        "mechanism": "cuda-criu",
        "default": "normal-load",
        "cache": receipt["cache"],
        "ready_boundary": "HTTP health after immutable model state and CUDA synchronization; request-specific compilation excluded",
        "clock_notes": [
            "Readiness is polled from kubectl exec; observation includes polling and transport overhead.",
            "Kubernetes container startedAt has one-second resolution; Pod-create request clock is client monotonic.",
            "The restored worker's model_load_seconds is the donor's historical loader time, never a restore measurement.",
            "First valid output includes deliberate post-readiness fixture localization, exec/validation overhead and full scientific computation.",
            "No image pull, node acquisition, disk-cache eviction or reserved-RAM guarantee was exercised by these matched pairs.",
        ],
        "parameters": {
            "dtype": "bf16",
            "cycles": 10,
            "steps": 200,
            "samples_per_seed": 1,
            "msa": "none",
            "templates": False,
            "rna": False,
        },
        "inputs": [
            {
                "residues": 42,
                "seeds": [101],
                "raw_sha256": "616cbd11f2c07e57c4e0c6ac121bfc891e593ec072946a577f921993c2e9f50e",
                "finite_atoms": 321,
            },
            {
                "residues": 76,
                "seeds": [101, 102],
                "raw_sha256": "550970e4f3d82513056a914b4d5c0e2bc1d59264aa17ce68bbee0ddad0fd93e9",
                "finite_atoms": 602,
            },
        ],
        "bundle": {
            "id": "protenix-v2-h100-cuda-criu-20260907-r2",
            "sha256": bundle["bundle_sha256"],
            "bytes": bundle["bytes"],
            "file_count": bundle["file_count"],
            "subpath": "protenix-r2",
            "captured_runtime_path": "/checkpoints/protenix-r2",
            "ownership": "per-file mode, UID and GID are bound into the canonical bundle manifest",
            "storage": "existing regional shared filesystem, read-only bundle; per-Pod mutable scratch",
        },
        "compatibility": bundle["compatibility"],
        "statistics": stats,
        "runs": runs,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--receipt", type=Path, required=True)
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = project(
        json.loads(args.receipt.read_bytes()), json.loads(args.bundle.read_bytes())
    )
    expected = result["compatibility"]["runtime_identity"]["snapshot_source_sha256"]
    for name, digest in expected.items():
        if hashlib.sha256((args.source / name).read_bytes()).hexdigest() != digest:
            raise ValueError(
                f"current source differs from captured compatibility: {name}"
            )
    result["cli_proxy_sha256"] = hashlib.sha256(
        (args.source / "protenix_cli_proxy.py").read_bytes()
    ).hexdigest()
    result["private_receipt_sha256"] = hashlib.sha256(
        args.receipt.read_bytes()
    ).hexdigest()
    args.output.write_text(json.dumps(result, indent=2) + "\n")


if __name__ == "__main__":
    main()
