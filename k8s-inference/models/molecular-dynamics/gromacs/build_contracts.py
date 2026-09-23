"""Project the canonical GROMACS request schema and unrouted candidate profile."""

import hashlib
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path.insert(0, str(HERE / "runtime"))
from fs2_gromacs import MODEL_ID, NVIDIA_IMAGE, PARAMETER_SCHEMA
from fs2_gromacs.contracts import canonical, request_schema


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + "\n")


def profile():
    revision = NVIDIA_IMAGE.rsplit(":", 1)[1]
    workload = {
        "stages": [{
            "id": "workflow", "needs": [], "resource_class": "gpu",
            "admission_mode": "independent-jobs", "min_parallelism": 1, "max_parallelism": 128,
            "checkpoint_mode": "resume", "preemption_mode": "checkpointable",
            "placement": {"class": "accelerator"},
            "resources": {
                "cpu_millis": 8000, "memory_bytes": 16 * 1024**3, "ephemeral_storage_bytes": 64 * 1024**3,
                "limits": {"cpu_millis": 8000, "memory_bytes": 16 * 1024**3, "ephemeral_storage_bytes": 64 * 1024**3},
            },
        }],
        "retry": {"max_attempts": 3, "retryable_exit_codes": [75, 137, 143]},
        "cancellation": {"mode": "terminate-attempt", "grace_seconds": 120},
    }
    runtime = b"".join(path.read_bytes() for path in sorted((HERE / "runtime/fs2_gromacs").glob("*.py")))
    return {
        "schema": "fs2-serve.nebius.ai/scientific-workload-profile/v1",
        "model_id": MODEL_ID, "display_name": "GROMACS · NVIDIA-optimized molecular dynamics",
        "execution_mode": "scientific-batch", "state": "candidate-unqualified", "route_exposed": False,
        "source": {"kind": "oci", "repository": "nvidia/gromacs", "revision": revision,
                   "review_url": "https://catalog.ngc.nvidia.com/orgs/nvidia/containers/gromacs",
                   "classification": "candidate-input"},
        "execution_identity": {"model_revision": revision, "runtime_image_digest": None,
            "runtime_recipe_sha256": hashlib.sha256(runtime).hexdigest(),
            "workload_recipe_sha256": hashlib.sha256(canonical(workload)).hexdigest(),
            "artifact_manifest_digest": None, "execution_identity_sha256": None},
        "interface": {"protocol": "scientific-batch-v1", "submit_endpoint": "/v1/models/gromacs:submit",
            "request_schema": "fs2-serve.nebius.ai/scientific-run-request/v1",
            "result_schema": "fs2-serve.nebius.ai/scientific-run-result/v1", "parameter_schema": PARAMETER_SCHEMA,
            "operations": ["run-workflow"], "service_classes": ["interactive", "customer-batch", "bulk-backfill"],
            "mcp": {"discoverable": True, "invocable": False, "tool_name": "submit_gromacs_workflow",
                    "description": "Run native NVIDIA GROMACS preparation, MD, minimization, analysis or free-energy workflows. Accepts an immutable input bundle and ordered commands; returns durable runs and trajectory/checkpoint artifacts."}},
        "access": {"profile": "standard", "state": "not-required", "receipt_digest": None, "credentials_embedded": False},
        "resources": {"gpu_count": 1, "gpu_topology": "single-gpu", "host_architectures": ["amd64"],
            "compatible_pool_ids": ["h100-ondemand-1x", "h100-1x", "h100-reserved-8x", "l40s-1x"],
            "required_node_labels": {"kubernetes.io/arch": "amd64"}},
        "workload": workload,
        "semantic_validation": {"validator_id": "gromacs-workflow-v1", "state": "candidate-unqualified"},
        "policy": {"commercial_use": "allowed", "non_clinical": False, "limitations": [
            "Unrouted candidate. Runtime tests do not qualify hosted REST/MCP, preemption or customer storage.",
            "Exact NVIDIA v2026.2 image identifies itself as GROMACS 2026.2-dev, CUDA 13, thread-MPI. Multi-node requires a separate external-MPI build.",
            "One GPU and up to eight OpenMP threads per job. Jobs are independent; coupled replica exchange is not implemented in this execution shape.",
            "All scientific settings and output cadence remain in customer inputs. Execution success is not proof of equilibration, convergence or force-field suitability.",
            "Colvars and PLUMED are compiled in; external kernel availability and enhanced-sampling correctness remain Priority 2. CP2K and Torch NNPot are not compiled in.",
            "Native checkpoint continuation is distinct from optional CUDA process snapshots; GPU snapshot acceleration has not been qualified.",
        ]},
    }


def main():
    schema = request_schema()
    write(ROOT / "catalog/runtime/schema/gromacs-workflow-request.schema.json", schema)
    write(ROOT / "components/control-plane/src/fs2_serve/model_input_schemas/gromacs-workflow.json", schema)
    write(HERE / "activation/workload-profile.json", {
        "schema": "fs2-serve.nebius.ai/scientific-workload-profile-projection/v1",
        "merge_target": "catalog/runtime/contracts/scientific-workload-profiles.json", "profile": profile(),
    })


if __name__ == "__main__":
    main()
