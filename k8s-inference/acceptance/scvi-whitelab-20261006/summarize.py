"""Export an allowlisted, credential-free summary of retained qualification data."""

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path


def read(root, name):
    return json.loads((root / name).read_text())


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def cohort(root, name, interface):
    directory = root / name
    status = read(directory, "status.json")
    operation = status["operation"]
    worker = read(directory, "worker-result.json")
    validation = read(directory, "output-validation.json")
    if (operation["tenant_id"], operation["principal_id"]) != ("system", "qa"):
        raise ValueError("Only internal QA evidence may be exported")
    if operation["status"] != "succeeded" or validation["status"] != "passed":
        raise ValueError("Incomplete or failed runs cannot qualify this report")
    if not (operation["id"] == worker["operation_id"] == validation["operation_id"]):
        raise ValueError("Mismatched receipts")
    accepted = datetime.fromisoformat(operation["accepted_at"])
    completed = datetime.fromisoformat(operation["completed_at"])
    result = {
        "operation_id": operation["id"],
        "interface": interface,
        "accepted_at": operation["accepted_at"],
        "completed_at": operation["completed_at"],
        "accepted_to_completed_seconds": (completed - accepted).total_seconds(),
        "worker_seconds": worker["elapsed_seconds"],
        "worker_timings_seconds": worker["timings_seconds"],
        "cells": worker["cells"],
        "genes": worker["genes"],
        "gpu": worker["gpu"],
        "peak_host_rss_bytes": worker["peak_host_rss_bytes"],
        "pytorch_peak_allocated_bytes_not_total_device_usage": worker[
            "peak_gpu_memory_bytes"
        ],
        "input_sha256": worker["input_sha256"],
        "versions": worker["versions"],
        "parameters": {
            key: worker["parameters"].get(key)
            for key in (
                "method",
                "mode",
                "resource_profile",
                "counts_source",
                "batch_key",
                "labels_key",
                "n_top_genes",
                "hvg_span",
                "max_epochs",
                "scanvi_max_epochs",
                "query_max_epochs",
                "batch_size",
                "seed",
                "visualization",
            )
        },
        "validation": validation,
        "attempts": [
            attempt
            for stage in status["batch"]["stages"]
            for attempt in stage["attempts"]
        ],
        "worker_receipt_sha256": digest(directory / "worker-result.json"),
        "status_receipt_sha256": digest(directory / "status.json"),
    }
    bucket_path = directory / "bucket-readback.json"
    if bucket_path.exists():
        bucket = read(directory, "bucket-readback.json")
        if bucket["operation_id"] != operation["id"]:
            raise ValueError("Mismatched bucket receipt")
        result["bucket_readback"] = {
            "metadata_verified_files": bucket["metadata_verified_files"],
            "direct_byte_readback": bucket["direct_byte_readback"],
            "receipt_sha256": digest(bucket_path),
        }
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    deployment = read(args.evidence, "final-live/deployment.json")
    image = deployment["spec"]["template"]["spec"]["containers"][0]["image"]
    execution = read(args.evidence, "final-live/execution.json")
    binding = next(
        item for item in execution["models"] if item["model_id"] == "scvi-scanvi"
    )
    worker_digest = binding["stages"][0]["image"].split("@")[1]
    if (
        image.split("@")[1]
        != "sha256:f64cd4b39a6eaf41e1762837e0a878ee7a78307f973ef6a822a29b118b5510df"
        or worker_digest
        != "sha256:063877787f8c1c1aef28887242389449b871e1d48c74edc07c267bccee340246"
    ):
        raise ValueError(
            "Captured release changed; do not attribute these cohorts to another release"
        )
    cancellation = read(args.evidence, "cancel-mcp-r6/status.json")["operation"]
    if (
        cancellation["tenant_id"],
        cancellation["principal_id"],
        cancellation["status"],
    ) != ("system", "qa", "cancelled"):
        raise ValueError("Cancellation was not verified")
    report = {
        "schema": "fs2-serve.nebius.ai/scvi-poc-qualification-summary/v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "scope": "Measured public-data PoC execution, not biological accuracy or unrestricted production readiness",
        "backend_image": image,
        "backend_build_commit": "96626104f8b04a8bf232371f1f43d35e07f3d5bd",
        "worker_digest": worker_digest,
        "execution_identity_sha256": binding["execution_identity_sha256"],
        "scheduling_sha256": digest(args.evidence / "final-live/scheduling.json"),
        "evidence_directory": str(args.evidence),
        "cohorts": [
            cohort(args.evidence, name, interface)
            for name, interface in (
                ("hlca-rest-r6", "REST"),
                ("atlas-mcp-r6", "typed MCP"),
                ("query-mcp-r6", "typed MCP: reference mapping"),
                ("scvi-mcp-r6", "typed MCP: scVI-only"),
                ("customer-cli-r6", "customer submit.py/collect.py"),
                ("recovery-r6/hosted", "MCP: worker SIGTERM and replacement"),
                ("eviction-r6/hosted", "REST: Kubernetes Pod eviction and replacement"),
            )
        ],
        "large_transfer": read(args.evidence, "fresh-transfer-r6/receipt.json"),
        "cancellation": {
            "operation_id": cancellation["id"],
            "status": cancellation["status"],
        },
        "public_routing_after_change": read(
            args.evidence, "artifact-route-r6b/public-routing-after.json"
        ),
        "not_claimed": [
            "WhiteLab-specific biological validation or convergence",
            "LibreChat agent qualification",
            "multi-GPU training",
            "GPU process snapshotting",
            "arbitrary dataset fit",
            "billing-grade GPU occupancy attribution",
            "full repository CI passing",
        ],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({"output": str(args.output), "cohorts": len(report["cohorts"])}))


if __name__ == "__main__":
    main()
