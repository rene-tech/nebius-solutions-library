#!/usr/bin/env python3
"""Reduce explicit startup markers into a secret-free, non-interchangeable table."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import statistics

MODELS = (
    "esmfold2",
    "esmfold2-fast",
    "protenix-v2",
    "alphafold3",
    "openfold3-openbind",
)


def utc(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def distribution(values):
    return {
        "n": len(values),
        "median": round(statistics.median(values), 6),
        "min": round(min(values), 6),
        "max": round(max(values), 6),
    }


def public_row(path):
    source = json.loads(path.read_bytes())
    assert source["status"] == "passed"
    ready = source["ready"]
    semantic = source["semantic_output"]
    begin = next(
        item for item in source["markers"] if item["phase"] == "original_command_start"
    )
    compute_end = next(
        item for item in source["markers"] if item["phase"] == "first_compute_complete"
    )
    compute_start = next(
        (item for item in source["markers"] if item["phase"] == "first_compute_start"),
        ready,
    )
    timing = {
        "pod_to_initialized_seconds": (
            utc(ready["utc"]) - utc(source["pod_created_at"])
        ).total_seconds(),
        "container_to_initialized_seconds": (
            utc(ready["utc"]) - utc(source["container_started_at"])
        ).total_seconds(),
        "python_to_initialized_seconds": ready["python_process_seconds"],
        "original_command_to_initialized_seconds": ready["monotonic_seconds"]
        - begin["monotonic_seconds"],
        "first_compute_including_lazy_compilation_seconds": compute_end[
            "monotonic_seconds"
        ]
        - compute_start["monotonic_seconds"],
        "container_to_valid_output_seconds": (
            utc(semantic["utc"]) - utc(source["container_started_at"])
        ).total_seconds(),
        "pod_to_valid_output_seconds": (
            utc(semantic["utc"]) - utc(source["pod_created_at"])
        ).total_seconds(),
        "initialized_to_valid_output_seconds": semantic["monotonic_seconds"]
        - ready["monotonic_seconds"],
    }
    assert all(value >= 0 for value in timing.values())
    structures = [
        {key: value[key] for key in ("sha256", "bytes", "atoms")}
        for value in semantic["structures"]
    ]
    assert structures and all(item["atoms"] > 0 for item in structures)
    return {
        "model_id": source["model_id"],
        "runtime_image_digest": source["image"].split("@", 1)[1],
        "unchanged_original_argv_sha256": begin["argv_sha256"],
        "pod_created_at": source["pod_created_at"],
        "container_started_at": source["container_started_at"],
        "initialized_at": ready["utc"],
        "valid_output_at": semantic["utc"],
        "initialized_monotonic_seconds": ready["monotonic_seconds"],
        "valid_output_monotonic_seconds": semantic["monotonic_seconds"],
        "initialization_boundary": ready["boundary"],
        "parameter_placement_at_initialized": ready["placement"],
        "compilation_state": ready["compilation"],
        "runtime_stack": {
            key: ready[key]
            for key in ("device", "compute_capability", "torch_version", "cuda_version")
            if key in ready
        },
        "timings": {key: round(value, 6) for key, value in timing.items()},
        "inputs": [
            {key: item[key] for key in ("sha256", "bytes")} for item in source["inputs"]
        ],
        "output_structures": structures,
        "normal_runtime_exit_code": semantic["unchanged_runtime_exit_code"],
        "private_receipt_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "private_log_sha256": hashlib.sha256(
            path.with_suffix("").with_suffix(".log").read_bytes()
        ).hexdigest(),
        "cache": source["cache"],
        "snapshot": "disabled",
        "outcome": "passed",
        "receipt_recovery": source.get("receipt_recovery"),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--private-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    rows = [
        public_row(path)
        for path in sorted(args.private_root.glob("*-isolated*/*.receipt.json"))
    ]
    models = []
    for model in MODELS:
        trials = sorted(
            [row for row in rows if row["model_id"] == model],
            key=lambda row: row["initialized_at"],
        )
        assert len(trials) == 3, (
            f"{model}: require exactly three successful physical process repetitions"
        )
        assert len({row["private_receipt_sha256"] for row in trials}) == 3
        models.append(
            {
                "model_id": model,
                "repetitions": trials,
                "summary_seconds": {
                    key: distribution([row["timings"][key] for row in trials])
                    for key in trials[0]["timings"]
                },
            }
        )
    document = {
        "schema": "fs2-serve.nebius.ai/current-structure-startup/v1",
        "measured_on": "2026-09-07",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "baseline_solution_source": "71547004",
        "deployed_platform_source": "adf1d8423e9b75afc5ba208baf53e479e7793922",
        "hardware": "NVIDIA H100 80GB HBM3; same reserved GPU pool and original resource requests/limits",
        "observed_driver_version": "580.159.04",
        "scenario": "Fresh isolated per-attempt process/workspace with existing localized model/reference cache; no page-cache eviction or snapshot restore",
        "public_request_clock": "Not measured by the isolated overlay; preceding normal public runs remain separate full-path correctness controls",
        "clock_resolution": "Model markers carry UTC and monotonic seconds; Kubernetes container/Pod timestamps have one-second resolution",
        "lazy_compilation": "Initialized is not fully shape-warmed. First compute includes lazy compilation and inference, without pretending they were separately profiled",
        "alphafold3_note": "Native initialization returns host parameters; JAX device placement and compilation happen on the first inference. Do not label its initialized clock GPU-ready",
        "valid_output_definition": "Unchanged full original command exited zero, then its complete structure files passed finite-coordinate/atom validation. This is the benchmark's earliest verified full output, not the timestamp of the first file write or public artifact delivery",
        "models": models,
    }
    args.output.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n")
    print(
        json.dumps(
            {model["model_id"]: model["summary_seconds"] for model in models}, indent=2
        )
    )


if __name__ == "__main__":
    main()
