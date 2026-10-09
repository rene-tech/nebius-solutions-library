"""Project engine-owned native MD contracts into unrouted platform candidates.

This offline generator neither enables a route nor qualifies an accelerator.
Use --check in CI to verify that public schemas match the worker normalizer.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from importlib import import_module
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
ENGINES = {
    "amber": {
        "repository": "fs2-platform/amber26-engine",
        "review_url": "https://ambermd.org/GetAmber.php",
        "display_name": "AMBER26 · PMEMD and AmberTools",
        "commercial_use": "license-dependent",
        "source_notes": (
            "Private PMEMD26/AmberTools26 OCI bundle built from the official academic PMEMD source. "
            "Operator confirms the AMBER agreement and academic use. Not an NVIDIA NIM; "
            "the source archive and licensed binaries remain private. Native and hosted qualification "
            "are separate evidence; user access uses the existing App grants."
        ),
        "description": (
            "Run AMBER26 PMEMD CUDA/CPU simulations and AmberTools preparation/analysis. "
            "Supply native MDIN, topology, coordinates and restraints; retrieve trajectories, "
            "energies, restart files and logs from Object Storage."
        ),
        "limitations": [
            "This deployment is for operator-approved academic use under the confirmed AMBER agreement. Other usage requires suitable licence coverage, not just access to an image.",
            "PMEMD CUDA SPFP and DPFP are distinct precision modes. AmberTools preparation and analysis use the pinned CPU-only conda-forge distribution.",
            "One GPU and one native process per job. Independent replicas and alchemical windows share the durable queue; coupled MPI/replica exchange is not qualified.",
            "Recovery commits the complete stopped workspace at successful native stage boundaries. An interrupted active stage retries from its preceding committed state; partial restart files are not silently accepted.",
            "Native restart does not imply serialization of stochastic state. Set scientifically appropriate seeds and validate continuation for the selected thermostat and sampling method.",
        ],
    },
    "lammps": {
        "repository": "nvidia/lammps",
        "display_name": "LAMMPS · NVIDIA-optimized molecular dynamics",
        "description": (
            "Run native LAMMPS preparation, minimization, dynamics and analysis using Kokkos-CUDA. "
            "Submit bundled scripts, inputs and potentials; receive native checkpoints, trajectories, "
            "thermodynamics and logs in customer Object Storage."
        ),
        "limitations": [
            "Pinned NVIDIA stable_22Jul2025 image. Installed packages and styles, not all upstream optional packages, define feature availability.",
            "Kokkos-CUDA and explicit CPU stages are packaged; the separate LAMMPS GPU package is not compiled into this image.",
            "One GPU and one MPI rank per job. Independent jobs scale through the shared queue; coupled multi-node execution is not qualified.",
            "Continuous recovery requires an explicit complete continuation script including fixes, computes, variables, potentials and outputs. Native restart files alone are insufficient.",
        ],
    },
    "namd": {
        "repository": "nvidia/namd",
        "display_name": "NAMD · NVIDIA-optimized molecular dynamics",
        "description": (
            "Run native NAMD preparation, minimization and dynamics with GPU-resident or offload execution. "
            "Submit bundled Tcl, topology and force-field inputs; receive native checkpoints, trajectories, "
            "energies and logs in customer Object Storage."
        ),
        "limitations": [
            "Operator confirms NVIDIA agreement coverage. The pinned accessible NVIDIA container carries NAMD 3.0.2, not the later upstream 3.0.3 fixes.",
            "GPU-resident and GPU-offload feature coverage differs; each scientific protocol needs an appropriate execution mode.",
            "One GPU and one native process per job. Independent jobs scale through the shared queue; multi-node Charm++ is not qualified.",
            "Wrapper-managed dynamics uses finite native segments; arbitrary complete Tcl is recovered only at completed stage boundaries.",
            "Upstream 3.0.3 correctness fixes affecting spinAngle/eABF and GBIS offload are not proven backported in this pinned image. Affected workflows remain unqualified.",
        ],
    },
}


def runtime(model: str):
    sys.path.insert(0, str(HERE / "gromacs/runtime"))
    sys.path.insert(0, str(HERE / model / "runtime"))
    return import_module(f"fs2_{model}"), import_module(f"fs2_{model}.contracts")


def profile(model: str) -> dict:
    engine, contracts = runtime(model)
    config = ENGINES[model]
    revision = engine.ENGINE_ID.rsplit(":", 1)[1]
    resources = {"cpu_millis": 8000, "memory_bytes": 16 * 1024**3, "ephemeral_storage_bytes": 64 * 1024**3}
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
                "resources": {**resources, "limits": resources},
            }
        ],
        "retry": {"max_attempts": 3, "retryable_exit_codes": [75, 137, 143]},
        "cancellation": {"mode": "terminate-attempt", "grace_seconds": 120},
    }
    recipe = b"".join(path.read_bytes() for path in sorted((HERE / model / "runtime" / f"fs2_{model}").glob("*.py")))
    return {
        "schema": "fs2-serve.nebius.ai/scientific-workload-profile/v1",
        "model_id": model,
        "display_name": config["display_name"],
        "execution_mode": "scientific-batch",
        "state": "candidate-unqualified",
        "route_exposed": False,
        "source": {
            "kind": "oci",
            "repository": config["repository"],
            "revision": revision,
            "review_url": config.get("review_url", f"https://catalog.ngc.nvidia.com/orgs/nvidia/containers/{model}"),
            "classification": "candidate-input",
        },
        "execution_identity": {
            "model_revision": revision,
            "runtime_image_digest": None,
            "runtime_recipe_sha256": hashlib.sha256(recipe).hexdigest(),
            "workload_recipe_sha256": hashlib.sha256(contracts.canonical(workload)).hexdigest(),
            "artifact_manifest_digest": None,
            "execution_identity_sha256": None,
        },
        "interface": {
            "protocol": "scientific-batch-v1",
            "submit_endpoint": f"/v1/models/{model}:submit",
            "request_schema": "fs2-serve.nebius.ai/scientific-run-request/v1",
            "result_schema": "fs2-serve.nebius.ai/scientific-run-result/v1",
            "parameter_schema": engine.PARAMETER_SCHEMA,
            "operations": ["run-workflow"],
            "service_classes": ["interactive", "customer-batch", "bulk-backfill"],
            "mcp": {
                "discoverable": True,
                "invocable": False,
                "tool_name": f"submit_{model}_workflow",
                "description": config["description"],
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
            "compatible_pool_ids": ["h100-ondemand-1x", "h100-1x", "h100-reserved-8x", "l40s-1x"],
            "required_node_labels": {"kubernetes.io/arch": "amd64"},
        },
        "workload": workload,
        "semantic_validation": {"validator_id": f"{model}-workflow-v1", "state": "candidate-unqualified"},
        "policy": {
            "commercial_use": config.get("commercial_use", "allowed"),
            "non_clinical": False,
            "limitations": [
                "Unrouted candidate. Runtime tests do not qualify hosted REST/MCP, recovery or customer storage.",
                *config["limitations"],
                "Scientific settings and output cadence remain customer controlled. Execution success is not evidence of equilibration or convergence.",
                "Native restart is distinct from GPU process snapshotting; GPU snapshot acceleration is not qualified.",
                "Individual files larger than 5 GiB are unqualified; choose output segments that fit the platform and customer bucket envelope.",
            ],
        },
    }


def outputs(model: str) -> dict[Path, dict]:
    _, contracts = runtime(model)
    schema = contracts.request_schema()
    candidate = profile(model)
    return {
        ROOT / f"catalog/runtime/schema/{model}-workflow-request.schema.json": schema,
        ROOT / f"components/control-plane/src/fs2_serve/model_input_schemas/{model}-workflow.json": schema,
        HERE / model / "activation/workload-profile.json": {
            "schema": "fs2-serve.nebius.ai/scientific-workload-profile-projection/v1",
            "merge_target": "catalog/runtime/contracts/scientific-workload-profiles.json",
            "profile": candidate,
        },
        HERE / model / "activation/source-candidate-receipt.json": {
            "model_id": model,
            "upstream_name": candidate["display_name"],
            "backend_identity": "native-upstream",
            "status": "candidate",
            "qualification_state": "unqualified",
            "observation_method": "pinned-runtime-artifact-manifest",
            "observed_on": "2026-09-23",
            "source": {key: value for key, value in candidate["source"].items() if key != "classification"},
            "access_profile": "standard",
            "access_state": "not-required",
            "notes": ENGINES[model].get("source_notes", (
                "Official NVIDIA NGC HPC distribution, not a NIM HTTP microservice. "
                "The source identity is pinned and inspected. Native scientific tests, hosted "
                "customer qualification, storage/recovery and GPU snapshots are separate evidence."
            )),
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", required=True, choices=sorted(ENGINES))
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    mismatches = []
    for path, value in outputs(args.model).items():
        text = json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False) + "\n"
        if args.check:
            if not path.is_file() or path.read_text() != text:
                mismatches.append(str(path.relative_to(ROOT)))
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
    if mismatches:
        raise SystemExit("Regenerate changed native contracts: " + ", ".join(mismatches))


if __name__ == "__main__":
    main()
