#!/usr/bin/env python3
"""Publish matched serving-snapshot measurements without private Pod specs."""

import argparse
import hashlib
import json
from datetime import datetime
from pathlib import Path
from statistics import median


def elapsed(start, end):
    return (
        datetime.fromisoformat(end.replace("Z", "+00:00"))
        - datetime.fromisoformat(start.replace("Z", "+00:00"))
    ).total_seconds()


def project(receipt):
    if receipt["status"] != "passed" or len(receipt["runs"]) != 6:
        raise ValueError("three complete normal/restore pairs are required")
    rows = []
    for run in receipt["runs"]:
        if run["status"] != "passed" or not run["gpu_pod_deleted"]:
            raise ValueError("trial must finish successfully and release its Pod")
        requests = run["semantics"]["requests"]
        if len(requests) != 2 or not all(item["passed"] for item in requests):
            raise ValueError("both inputs must produce valid full output")
        row = {
            key: run[key]
            for key in (
                "mode",
                "repetition",
                "pod_uid",
                "created_request_at",
                "pod_created_at",
                "container_started_at",
                "ready_observed_at",
                "pod_create_request_to_ready_seconds",
                "both_full_outputs_observed_at",
            )
        }
        row.update(
            container_start_to_ready_seconds=elapsed(
                run["container_started_at"], run["ready_observed_at"]
            ),
            container_start_to_both_full_outputs_seconds=elapsed(
                run["container_started_at"], run["both_full_outputs_observed_at"]
            ),
            requests=requests,
            gpu_pod_deleted=True,
        )
        rows.append(row)
    statistics = {}
    for mode in ("normal", "restore"):
        selected = [row for row in rows if row["mode"] == mode]
        if len(selected) != 3 or {row["repetition"] for row in selected} != {1, 2, 3}:
            raise ValueError("three distinct repetitions per mode are required")
        statistics[mode] = {}
        for clock in (
            "pod_create_request_to_ready_seconds",
            "container_start_to_ready_seconds",
            "container_start_to_both_full_outputs_seconds",
        ):
            values = [row[clock] for row in selected]
            statistics[mode][clock] = {
                "n": 3,
                "median_seconds": median(values),
                "min_seconds": min(values),
                "max_seconds": max(values),
            }
    result = {
        "schema": "fs2-serve.nebius.ai/serving-snapshot-qualification/v1",
        "model_id": receipt["model"],
        "status": "fresh-pod-qualified",
        "date": "2026-09-07",
        "mechanism": "cuda-criu",
        "default": "normal-load",
        "production_selectable": False,
        "qualification_scope": "isolated exact-image serving worker; production renderer and fallback rollout are separate",
        "cache": receipt["cache"],
        "ready_boundary": "application HTTP health after supervisor completed CUDA+CRIU restoration; normal trials use the original server health endpoint",
        "clock_notes": [
            "Every trial starts a fresh Pod; restore cannot pass from HTTP health alone before CUDA completion.",
            "Kubernetes container timestamps have one-second resolution; Pod-create request clock is client monotonic.",
            "Health polling includes kubectl transport and observation delay. First input may still compile request-specific kernels.",
            "Both-full-outputs clock includes two sequential inputs and client validation; it is not first-output latency or a pure inference benchmark.",
            "Existing image, weight and shared-filesystem caches are retained. No host cache eviction, node acquisition or reserved-RAM guarantee.",
            "The optional bridge changes only snapshot compatibility plumbing, not model image, precision, GPU memory utilization, output shape or generation steps.",
        ],
        "statistics": statistics,
        "runs": rows,
    }
    if receipt["model"] == "nv-reason-cxr-3b":
        result["semantic_scope"] = (
            "original two pinned non-clinical X-rays, also used before capture; not unseen-input evidence"
        )
        result["clock_notes"].append(
            "CXR restores retain the donor's original-fixture prefix/encoder cache state; output latency is not a controlled cold-input inference comparison."
        )
    elif receipt["model"] == "nv-segment-ct":
        result["semantic_scope"] = (
            "original two pinned synthetic non-clinical CT masks, also used before capture; "
            "not unseen-input evidence"
        )
    elif receipt["model"] == "sdxl":
        result["semantic_scope"] = (
            "original two pinned 512x512 prompts at seeds 2407 and 2408, also used "
            "before capture; not unseen-input evidence"
        )
    elif receipt["model"] == "genmol":
        result["semantic_scope"] = (
            "original two pinned QED and LogP requests, also used before capture; "
            "not unseen-input evidence"
        )
    elif receipt["model"] == "openfold3":
        result["semantic_scope"] = (
            "original two standalone Preview2 20-aa request IDs, also used before capture; "
            "not unseen-input evidence or the OpenBind scientific profile"
        )
        result["clock_notes"].append(
            "OpenFold3 retains the exact native bash activation, working directory and UID/GID; "
            "only the snapshot supervisor PATH includes the image's pinned conda Python."
        )
    elif receipt["model"] == "diffdock":
        result["semantic_scope"] = (
            "original pinned RCSB 1UBQ receptor and aspirin ligand at accepted random "
            "seeds 2370 and 2371, also used before capture; not unseen-input evidence"
        )
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--receipt", type=Path, required=True)
    parser.add_argument("--compatibility", type=Path, required=True)
    parser.add_argument("--publication", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = project(json.loads(args.receipt.read_bytes()))
    publication = json.loads(args.publication.read_bytes())
    if publication["status"] != "passed":
        raise ValueError(
            "snapshot publication must verify captured content and metadata"
        )
    result["compatibility"] = json.loads(args.compatibility.read_bytes())
    canonical = json.dumps(
        publication["files"], sort_keys=True, separators=(",", ":")
    ).encode()
    result["bundle"] = {
        "manifest_schema": "captured-content-and-filesystem-metadata/v1",
        "manifest_sha256": hashlib.sha256(canonical).hexdigest(),
        "bytes": sum(item.get("bytes", 0) for item in publication["files"]),
        "entry_count": len(publication["files"]),
        "storage": "existing shared filesystem; read-only captured bundle, private per-Pod writable scratch",
    }
    result["private_receipt_sha256"] = hashlib.sha256(
        args.receipt.read_bytes()
    ).hexdigest()
    result["publication_receipt_sha256"] = hashlib.sha256(
        args.publication.read_bytes()
    ).hexdigest()
    args.output.write_text(json.dumps(result, indent=2) + "\n")


if __name__ == "__main__":
    main()
