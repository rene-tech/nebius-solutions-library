"""Project the canonical GROMACS request schema and unrouted candidate profile."""

import hashlib
import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path.insert(0, str(HERE / "runtime"))
from fs2_gromacs import (
    MODEL_ID,
    MPI_ENGINE,
    MPI_PARAMETER_SCHEMA,
    NVIDIA_IMAGE,
    PARAMETER_SCHEMA,
)
from fs2_gromacs.contracts import canonical, request_schema


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + "\n")


def mpi_execution_shapes():
    """Operator envelopes; each runtime/hardware shape still needs live evidence."""
    shapes = []
    for nodes_mode in ("single-node", "multi-node"):
        for gpus in (1, 2, 4, 8):
            pools = ["h100-reserved-8x"]
            if gpus == 1:
                pools.insert(0, "h100-ondemand-1x")
                pools.append("l40s-1x")
            if gpus <= 4:
                pools.append("l40s-4x")
            requests = {
                "cpu_millis": 8000 * gpus,
                "memory_bytes": 16 * 1024**3 * gpus,
                "ephemeral_storage_bytes": 64 * 1024**3,
            }
            single = nodes_mode == "single-node"
            shapes.append({
                "id": f"{nodes_mode}-{gpus}gpu",
                "admission_mode": "independent-jobs" if single else "gang-jobset",
                "min_parallelism": 1 if single else 2,
                "max_parallelism": 1 if single else min(8, 16 // gpus),
                "placement": {
                    "class": "accelerator",
                    "accelerator": {"resource_name": "nvidia.com/gpu", "count": gpus, "pool_ids": pools},
                },
                "resources": {**requests, "limits": dict(requests)},
            })
    return shapes


def profile(*, mpi=False):
    revision = NVIDIA_IMAGE.rsplit(":", 1)[1]
    workload = {
        "stages": [
            {
                "id": "workflow",
                "needs": [],
                "resource_class": "gpu",
                "admission_mode": "independent-jobs",
                "min_parallelism": 1,
                "max_parallelism": 128,
                "checkpoint_mode": "resume",
                "preemption_mode": "checkpointable",
                "placement": {"class": "accelerator"},
                "resources": {
                    "cpu_millis": 8000,
                    "memory_bytes": 16 * 1024**3,
                    "ephemeral_storage_bytes": 64 * 1024**3,
                    "limits": {
                        "cpu_millis": 8000,
                        "memory_bytes": 16 * 1024**3,
                        "ephemeral_storage_bytes": 64 * 1024**3,
                    },
                },
            }
        ],
        "retry": {"max_attempts": 3, "retryable_exit_codes": [75, 137, 143]},
        "cancellation": {"mode": "terminate-attempt", "grace_seconds": 120},
    }
    runtime = b"".join(
        path.read_bytes()
        for path in sorted((HERE / "runtime/fs2_gromacs").glob("*.py"))
    )
    value = {
        "schema": "fs2-serve.nebius.ai/scientific-workload-profile/v1",
        "model_id": MODEL_ID,
        "display_name": "GROMACS · NVIDIA-optimized molecular dynamics",
        "execution_mode": "scientific-batch",
        "state": "candidate-unqualified",
        "route_exposed": False,
        "source": {
            "kind": "oci",
            "repository": "nvidia/gromacs",
            "revision": revision,
            "review_url": "https://catalog.ngc.nvidia.com/orgs/nvidia/containers/gromacs",
            "classification": "candidate-input",
        },
        "execution_identity": {
            "model_revision": revision,
            "runtime_image_digest": None,
            "runtime_recipe_sha256": hashlib.sha256(runtime).hexdigest(),
            "workload_recipe_sha256": hashlib.sha256(canonical(workload)).hexdigest(),
            "artifact_manifest_digest": None,
            "execution_identity_sha256": None,
        },
        "interface": {
            "protocol": "scientific-batch-v1",
            "submit_endpoint": "/v1/models/gromacs:submit",
            "request_schema": "fs2-serve.nebius.ai/scientific-run-request/v1",
            "result_schema": "fs2-serve.nebius.ai/scientific-run-result/v1",
            "parameter_schema": PARAMETER_SCHEMA,
            "operations": ["run-workflow"],
            "service_classes": ["interactive", "customer-batch", "bulk-backfill"],
            "mcp": {
                "discoverable": True,
                "invocable": False,
                "tool_name": "submit_gromacs_workflow",
                "description": "Run NVIDIA GROMACS preparation, MD, free-energy and analysis, with Colvars or PLUMED. Submit a bundled input and native commands; receive durable checkpoints and trajectory artifacts. Not a docking pose-search tool.",
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
            "compatible_pool_ids": [
                "h100-ondemand-1x",
                "h100-1x",
                "h100-reserved-8x",
                "l40s-1x",
            ],
            "required_node_labels": {"kubernetes.io/arch": "amd64"},
        },
        "workload": workload,
        "semantic_validation": {
            "validator_id": "gromacs-workflow-v1",
            "state": "candidate-unqualified",
        },
        "policy": {
            "commercial_use": "allowed",
            "non_clinical": False,
            "limitations": [
                "Unrouted candidate. Runtime tests do not qualify hosted REST/MCP, preemption or customer storage.",
                "Exact NVIDIA v2026.2 image identifies itself as GROMACS 2026.2-dev, CUDA 13, thread-MPI. Multi-node requires a separate external-MPI build.",
                "One GPU and up to eight OpenMP threads per job. Jobs are independent; coupled replica exchange is not implemented in this execution shape.",
                "All scientific settings and output cadence remain in customer inputs. Execution success is not proof of equilibration, convergence or force-field suitability.",
                "Colvars and PLUMED 2.10 are packaged in the enhanced runtime. Customer protocols and bias convergence need scientific validation. CP2K and Torch NNPot are separate builds, not capabilities of this App.",
                "Native checkpoint continuation is distinct from optional CUDA process snapshots; GPU snapshot acceleration has not been qualified.",
            ],
        },
    }
    if mpi:
        value.update(
            model_id="gromacs-mpi", display_name="GROMACS · distributed CUDA MPI"
        )
        value["source"] = {
            "kind": "git",
            "repository": "gromacs/gromacs",
            "revision": MPI_ENGINE.split("@")[1],
            "review_url": "https://gitlab.com/gromacs/gromacs/-/tree/" + MPI_ENGINE.split("@")[1],
            "classification": "candidate-input",
        }
        value["execution_identity"]["model_revision"] = MPI_ENGINE.split("@")[1]
        value["interface"].update(
            submit_endpoint="/v1/models/gromacs-mpi:submit",
            parameter_schema=MPI_PARAMETER_SCHEMA,
        )
        value["interface"]["mcp"].update(
            tool_name="submit_gromacs_mpi_workflow",
            description="Run one external-MPI GROMACS workflow on one to eight nodes with one, two, four or eight GPUs per node, up to sixteen total. Each GPU has one MPI rank. Returns durable native checkpoints and customer-bucket results.",
        )
        value["resources"].update(
            gpu_topology="multi-node",
            compatible_pool_ids=["h100-ondemand-1x", "h100-reserved-8x"],
        )
        stage = value["workload"]["stages"][0]
        stage.update(admission_mode="gang-jobset", min_parallelism=2, max_parallelism=8)
        stage["placement"]["accelerator"] = {
            "resource_name": "nvidia.com/gpu",
            "count": 1,
            "pool_ids": list(value["resources"]["compatible_pool_ids"]),
        }
        stage["execution_shapes"] = mpi_execution_shapes()
        value["semantic_validation"]["validator_id"] = "gromacs-mpi-workflow-v1"
        value["execution_identity"]["workload_recipe_sha256"] = hashlib.sha256(
            canonical(value["workload"])
        ).hexdigest()
        value["policy"]["limitations"] = [
            "Unrouted candidate. Distributed runtime probes do not qualify customer execution or recovery.",
            "Separate upstream GROMACS 2026.2 CUDA/Open MPI build, not the NVIDIA NGC engine binary.",
            "One to eight nodes, one/two/four/eight GPUs per node and at most sixteen GPUs total. One external MPI rank per GPU and up to eight CPU threads per rank. Multi-node work is gang-admitted; one-node work uses one Pod. No coupled replica exchange in these shapes.",
            "Execution shape allowlists describe candidate hardware capability, not completed qualification. Every shape, including L40S external MPI, needs evidence for the exact runtime digest before a supported-performance claim.",
            "Portable TCP host-staged MPI baseline. RDMA/direct GPU communication requires a separately qualified network profile.",
            "Native .cpt recovery is distinct from persistent GPU-process snapshots. Scientific convergence is not inferred from execution success.",
        ]
    return value


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--schemas-only", action="store_true", help="Regenerate request contracts without changing operator-owned profiles.")
    args = parser.parse_args()
    schema = request_schema()
    write(ROOT / "catalog/runtime/schema/gromacs-workflow-request.schema.json", schema)
    write(
        ROOT
        / "components/control-plane/src/fs2_serve/model_input_schemas/gromacs-workflow.json",
        schema,
    )
    if not args.schemas_only:
        write(
            HERE / "activation/workload-profile.json",
            {
                "schema": "fs2-serve.nebius.ai/scientific-workload-profile-projection/v1",
                "merge_target": "catalog/runtime/contracts/scientific-workload-profiles.json",
                "profile": profile(),
            },
        )
    write(
        ROOT / "catalog/runtime/schema/gromacs-mpi-workflow-request.schema.json",
        request_schema(mpi=True),
    )
    write(
        ROOT
        / "components/control-plane/src/fs2_serve/model_input_schemas/gromacs-mpi-workflow.json",
        request_schema(mpi=True),
    )
    if not args.schemas_only:
        write(
            HERE / "activation/mpi-workload-profile.json",
            {
                "schema": "fs2-serve.nebius.ai/scientific-workload-profile-projection/v1",
                "merge_target": "catalog/runtime/contracts/scientific-workload-profiles.json",
                "profile": profile(mpi=True),
            },
        )


if __name__ == "__main__":
    main()
