"""Read-only native candidate report; no cloud calls, submissions or promotion.

Existing-helper compatibility projection is additive and labels its missing
preflight occupancy capture. It never rewrites the original qualification.
"""
import argparse
import csv
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import statistics

from qualify_candidate import save, sha

CASES = {
    "single-h100-corrected": "single-corrected",
    "single-l40s-corrected": "single-corrected",
    "mpi1-h100-corrected": "mpi1-corrected",
    "mpi2-l40s-corrected": "mpi2-corrected",
    "mpi4-l40s-corrected": "mpi4-corrected",
    "mpi2x1-h100-corrected": "mpi2x1-corrected",
}


def read(path):
    return json.loads(path.read_text())


def reference(path, root):
    return {"path": str(path.relative_to(root)), "sha256": sha(path), "size_bytes": path.stat().st_size}


def legacy_projection(directory):
    """Use observed image IDs, not a requested-image-only compatibility claim."""
    raw = read(directory / "qualification.json")
    identity = read(directory / "identity-cleanup.json")
    validation = read(directory / "final-state-validation.json")
    result = read(directory / "workspace/result.json")
    image = raw["image"]
    if (identity["image"] != image or validation["status"] != "passed"
            or validation["native_result_sha256"] != sha(directory / "workspace/result.json")
            or raw["worker_exit_code"] != 0 or result["status"] != "succeeded"):
        raise ValueError("legacy proof is not passed/source-bound")
    ids = [c["image_id"] for p in identity["pods"] for c in p["images"] if c["name"] == "runtime"]
    if len(ids) != raw["gpu_count"] or any(i != image for i in ids):
        raise ValueError("legacy runtime images were not observed as the exact candidate")
    projection = {**raw, "source_revision": identity["source_revision"], "validation": validation,
                  "gpus": raw["gpu_count"], "image_id": ids[0], "all_observed_image_ids": ids,
                  "allowed_image_digests": [image.rsplit("@", 1)[1]],
                  "input_sha256": identity["input_sha256"], "request_sha256": identity["request_sha256"],
                  "finished_at": result["finished_at"], "cleanup": identity["cleanup"],
                  "derived_receipt": True,
                  "provenance": [reference(directory / name, directory) for name in
                                 ("qualification.json", "identity-cleanup.json", "final-state-validation.json")],
                  "limitations": ["No separate legacy preflight occupancy snapshot retained; pool is from the original helper qualification."]}
    # Compatibility file name is imposed by the existing release binder; do not
    # fabricate a historical capacity reading to fill that interface.
    capacity = {"labels": {"accelerator.fs2.nebius/pool-id": raw["pool"]},
                "capture_scope": "derived pool identity only; NOT a preflight occupancy capture",
                "free_by_requests": None, "allocatable_gpus": None, "existing_gpu_pods": None,
                "source": reference(directory / "qualification.json", directory)}
    return projection, capacity


def gpu_observations(directory):
    files = [directory / "gpu.txt"] if (directory / "gpu.txt").exists() else sorted(directory.glob("*-gpu.txt"))
    records = []
    for path in files:
        with path.open() as stream:
            for row in csv.DictReader(stream, skipinitialspace=True):
                records.append({k: row[k] for k in ("uuid", "name", "driver_version", "compute_cap")})
    return records


def case_record(root, case, fixture):
    directory = root / case
    receipt = read(directory / "receipt.json")
    validation_path = directory / "final-state-validation.json"
    validation = read(validation_path)
    result_path = directory / "workspace/result.json"
    result = read(result_path)
    if validation["status"] != "passed" or validation["native_result_sha256"] != sha(result_path):
        raise ValueError("invalid/changed final-state validation: " + case)
    fixture_dir = root / "fixtures" / fixture
    if receipt["input_sha256"] != sha(fixture_dir / "input.tar.gz") or receipt["request_sha256"] != sha(fixture_dir / "request.json"):
        raise ValueError("changed fixture: " + case)
    runs = [c for c in result["commands"] if "mdrun" in c["command"]]
    rates = [c["performance_ns_per_day"] for c in runs]
    topology = directory / "workspace/.fs2/mpi-topology.json"
    if not topology.exists():
        topology = directory / "workspace/mpi-topology.json"
    topology_value = read(topology) if topology.exists() else None
    version = re.search(r"^GROMACS version:\s*(.+)$", result["engine"], re.M)
    return {
        "case": case, "status": "passed", "image": receipt["image"], "source_revision": receipt["source_revision"],
        "pool": read(directory / "capacity-before.json")["labels"]["accelerator.fs2.nebius/pool-id"],
        "nodes": result.get("nodes", 1), "gpus_per_node": result.get("gpus_per_node", receipt["gpus"]),
        "engine_version": version[1] if version else None, "gpu_observations": gpu_observations(directory),
        "observed_image_ids": receipt.get("all_observed_image_ids", [receipt["image_id"]]),
        "runtime_wall_seconds": receipt["runtime_wall_seconds"],
        "native_ns_per_day": {"repeats": rates, "mean": statistics.mean(rates), "sample_stdev": statistics.stdev(rates)},
        "native_command_wall_seconds": [c["wall_seconds"] for c in runs],
        "checkpoint_steps": [c["checkpoint_step"] for c in runs],
        "rank_bindings": [[{k: b[k] for k in ("world_rank", "local_rank", "host", "gpu_uuid", "gromacs_gpu_id", "threads_per_rank")}
                           for b in c.get("rank_bindings", [])] for c in runs],
        "transport": topology_value, "cleanup": receipt["cleanup"],
        "artifact_inventory_count": len(result["files"]),
        "checks": validation["checks"], "independent_checkpoint_reads": validation.get("independent_checkpoint_reads"),
        "evidence": [reference(p, root) for p in (directory / "receipt.json", validation_path, result_path,
                                                  fixture_dir / "input.tar.gz", fixture_dir / "request.json")],
        "limitations": receipt.get("limitations", []),
    }


def report(root):
    records = [case_record(root, case, fixture) for case, fixture in CASES.items()]
    diagnostic = root / "diagnostic-h100"
    correction = read(diagnostic / "validation-correction.json")
    if correction["status"] != "passed" or correction["native_result_sha256"] != sha(diagnostic / "workspace/result.json"):
        raise ValueError("diagnostic correction must bind the unchanged native result")
    return {
        "schema": "fs2-serve.nebius.ai/gromacs-candidate-regression/v1", "status": "passed",
        "recorded_at": datetime.now(timezone.utc).isoformat(), "customer_ready": False,
        "customer_paths_tested": False, "production_mutations": False, "gpu_snapshot_used": False,
        "scope": "Exactly the six bounded MEM native execution shapes and one expected output-budget diagnostic check listed here.",
        "tests": records,
        "expected_failure": {"case": "diagnostic-h100", "status": "passed", "native_outcome": "failed",
                             "evidence": [reference(diagnostic / p, root) for p in
                                          ("receipt.json", "validation-correction.json", "workspace/result.json")],
                             "note": "Original validator wrongly required committed generation < native generation. Both were zero before first checkpoint; unchanged artifact correction is retained separately."},
        "retained_failures": [{"case": case, "evidence": reference(root / case / "receipt.json", root),
                               "cause": "Obsolete forced counter reset hit PME-tuning fatal error at step 5000; original science bundle unchanged in corrected requests."}
                              for case in ("single-h100", "single-l40s")],
        "unmatched_context": {"case": "mpi1-l40s", "evidence": reference(root / "mpi1-l40s/receipt.json", root),
                              "note": "Passed with the obsolete forced reset and explicit CPU PME; not pooled with corrected no-reset MPI timings."},
        "limitations": [
            "Three 10000-step/20-ps repeats per shape include warm-up; no completed PME/DLB tuning or steady-state scaling claim.",
            "Single NGC and separately compiled external-MPI engines differ; timing ratios are not a matched-engine speedup claim.",
            "MPI controls explicitly use CPU PME (-pme cpu -npme 0) and CPU update for original all-bond constraints; nonbonded work uses GPUs.",
            "Native final energy has one sample per repeat; finite final coordinates/cells and checkpoints do not prove ensemble convergence.",
            "Configured UCX-local or host-staged TCP is recorded; transport_observed is null. No RDMA/bandwidth claim.",
            "PMIx compression-library warnings and CPU-quota/pinning warnings remain in raw logs; no silent runtime repair.",
            "GPU allocation and CUDA rank dispatch do not imply continuous GPU compute utilization.",
            "No new 8/16-GPU, hosted API/MCP, cancellation/preemption, artifact transport, or GPU-process-snapshot acceptance here.",
        ],
        "source": [{"path": str(Path(__file__).name), "sha256": sha(__file__)},
                   {"path": "qualify_candidate.py", "sha256": sha(Path(__file__).with_name("qualify_candidate.py"))}],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--project-legacy", action="store_true")
    args = parser.parse_args()
    if args.project_legacy:
        directory = args.evidence_root / "mpi2x1-h100-corrected"
        receipt, capacity = legacy_projection(directory)
        save(directory / "receipt.json", receipt)
        save(directory / "capacity-before.json", capacity)
    value = report(args.evidence_root)
    save(args.output, value)
    print(json.dumps({"output": str(args.output), "sha256": sha(args.output), "status": value["status"],
                      "native_shapes": len(value["tests"]), "customer_ready": False}))


if __name__ == "__main__":
    main()
