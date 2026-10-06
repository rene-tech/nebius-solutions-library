"""Generate unrouted profile and API/MCP schemas from the worker contract.

This is an authoring projection only; it does not publish or qualify a model.
Run with --check in CI to detect schema drift.
"""

import argparse
import copy
import hashlib
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path.insert(0, str(HERE / "runtime"))
from fs2_scvi import PARAMETER_SCHEMA
from fs2_scvi.contracts import canonical, request_schema

SOURCE_REVISION = "56520c713eb1d2b245c72a0f60bc393b74198c91"


def resources(gib):
    value = {
        "cpu_millis": 8000,
        "memory_bytes": gib * 1024**3,
        "ephemeral_storage_bytes": 128 * 1024**3,
    }
    return {**value, "limits": dict(value)}


def profile():
    pools = ["h100-1x", "h100-ondemand-1x", "h100-reserved-8x"]
    stage = {
        "id": "workflow",
        "needs": [],
        "resource_class": "gpu",
        "admission_mode": "independent-jobs",
        "min_parallelism": 1,
        "max_parallelism": 1,
        "checkpoint_mode": "resume",
        "preemption_mode": "checkpointable",
        "placement": {"class": "accelerator"},
        "resources": resources(128),
        "execution_shapes": [],
    }
    for name, ram, allowed in (
        ("routine", 128, pools),
        ("atlas", 256, ["h100-reserved-8x"]),
    ):
        stage["execution_shapes"].append(
            {
                "id": name,
                "admission_mode": "independent-jobs",
                "min_parallelism": 1,
                "max_parallelism": 1,
                "placement": {
                    "class": "accelerator",
                    "accelerator": {
                        "resource_name": "nvidia.com/gpu",
                        "count": 1,
                        "pool_ids": allowed,
                    },
                },
                "resources": resources(ram),
            }
        )
    workload = {
        "stages": [stage],
        "retry": {"max_attempts": 3, "retryable_exit_codes": [75, 137, 143]},
        "cancellation": {"mode": "terminate-attempt", "grace_seconds": 120},
    }
    recipe = [
        {
            "path": str(path.relative_to(ROOT)),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        }
        for path in sorted((HERE / "runtime/fs2_scvi").glob("*.py"))
    ]
    return {
        "schema": "fs2-serve.nebius.ai/scientific-workload-profile/v1",
        "model_id": "scvi-scanvi",
        "display_name": "scVI / scANVI · single-cell integration and annotation",
        "execution_mode": "scientific-batch",
        "state": "candidate-unqualified",
        "route_exposed": False,
        "source": {
            "kind": "git",
            "repository": "scverse/scvi-tools",
            "revision": SOURCE_REVISION,
            "review_url": "https://github.com/scverse/scvi-tools/tree/1.5.0.post1",
            "classification": "candidate-input",
        },
        "execution_identity": {
            "model_revision": SOURCE_REVISION,
            "runtime_image_digest": None,
            "runtime_recipe_sha256": hashlib.sha256(canonical(recipe).rstrip(b"\n")).hexdigest(),
            "workload_recipe_sha256": hashlib.sha256(canonical(workload).rstrip(b"\n")).hexdigest(),
            "artifact_manifest_digest": None,
            "execution_identity_sha256": None,
        },
        "interface": {
            "protocol": "scientific-batch-v1",
            "submit_endpoint": "/v1/models/scvi-scanvi:submit",
            "request_schema": "fs2-serve.nebius.ai/scientific-run-request/v1",
            "result_schema": "fs2-serve.nebius.ai/scientific-run-result/v1",
            "parameter_schema": PARAMETER_SCHEMA,
            "operations": ["fit-transform"],
            "service_classes": ["customer-batch", "bulk-backfill"],
            "mcp": {
                "discoverable": True,
                "invocable": False,
                "tool_name": "submit_scvi_scanvi",
                "description": "Queue raw-count AnnData for scVI integration or scANVI annotation/reference mapping. "
                "Choose training and memory settings; retrieve embeddings, labels/probabilities, reference, "
                "learning curves and plots via the returned operation.",
            },
        },
        "access": {
            "profile": "standard",
            "state": "not-required",
            "receipt_digest": None,
            "credentials_embedded": False,
        },
        "resources": {
            "gpu_count": 1,
            "gpu_topology": "single-gpu",
            "host_architectures": ["amd64"],
            "compatible_pool_ids": pools,
            "required_node_labels": {"kubernetes.io/arch": "amd64"},
        },
        "workload": workload,
        "semantic_validation": {
            "validator_id": "scvi-scanvi-workflow-v1",
            "state": "candidate-unqualified",
        },
        "policy": {
            "commercial_use": "allowed",
            "non_clinical": True,
            "limitations": [
                "Unrouted candidate: real GPU and hosted API/MCP qualification are required before customer use.",
                "500k/1M cells and 10/25 GiB inputs are qualification targets, not measured limits.",
                "Input memory, preprocessing and labels determine suitability. Resource profiles do not guarantee convergence.",
                "Only the selected raw-count matrix and genes are exported; the immutable full input remains available.",
                "Full training-state checkpoint recovery is distinct from GPU process snapshotting; no GPU snapshot speed claim.",
                "The existing small native route is separate and retains its published bounds until deliberately migrated.",
            ],
        },
    }


def outputs():
    schema = request_schema()
    schema["$id"] = "https://fs2-serve.nebius.ai/schema/scvi-workflow-request/v1"
    return {
        ROOT / "catalog/runtime/schema/scvi-workflow-request.schema.json": schema,
        ROOT
        / "components/control-plane/src/fs2_serve/model_input_schemas/scvi-workflow.json": copy.deepcopy(
            schema
        ),
        HERE / "activation/workload-profile.json": {
            "schema": "fs2-serve.nebius.ai/scientific-workload-profile-projection/v1",
            "merge_target": "catalog/runtime/contracts/scientific-workload-profiles.json",
            "profile": profile(),
        },
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    for path, value in outputs().items():
        encoded = json.dumps(value, indent=2) + "\n"
        if args.check:
            if not path.exists() or path.read_text() != encoded:
                raise SystemExit(f"Generated contract drift: {path}")
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(encoded)


if __name__ == "__main__":
    main()
