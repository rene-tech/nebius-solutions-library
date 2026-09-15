#!/usr/bin/env python3
"""Build Protenix comparison from retained attempts, without hiding bad cohorts."""
from datetime import datetime, timezone
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
CURRENT_IMAGE = "ac8f7c2c35d2bc911281f9d4a8aa9779e2cb955cdb1c2c2d37eb31d89669980e"
BIR_IMAGE = "0c391bdc2ab0c5260a222ec7df5e5adc5ea9bfdbc2e158b386a97aac5dc72e94"


def main():
    analysis = json.loads((HERE / "analysis.json").read_text())
    allocation = json.loads((HERE / "allocation.json").read_text())
    summaries = {(row["variant"], row["case"]): row for row in analysis["summaries"]}
    cases = ("ubiquitin-76", "lysozyme-129", "lysozyme-homodimer-258")
    comparisons = []
    for case in cases:
        variants = {}
        for variant in ("current-conventional", "current-persistent", "bir-graphs-global-rng",
                        "bir-graphs", "bir-eager-private-seed"):
            row = summaries.get((variant, case))
            if row:
                variants[variant] = {
                    "attempts": row["attempts"], "valid": row["valid"],
                    "first_request_seconds": row["first_shape_request_seconds"],
                    "median_request_seconds": (row["median_wall_seconds"]
                        if variant == "current-conventional" else row["median_subsequent_wall_seconds"]),
                    "median_forward_seconds": (row["median_forward_seconds"]
                        if variant == "current-conventional" else row["median_subsequent_forward_seconds"]),
                    "warm_repetitions": row["subsequent_requests"],
                    "median_reference_chain_ca_lddt": row["median_chain_ca_lddt"],
                }
        candidate = variants.get("bir-graphs-global-rng", {}).get("median_request_seconds")
        ratios = {variant: value["median_request_seconds"] / candidate
                  for variant, value in variants.items()
                  if candidate and value["median_request_seconds"] and variant.startswith("current-")}
        comparisons.append({"case": case, "gpu": "H100 80GB HBM3", "variants": variants,
                            "ratio_to_primary_bir": ratios})
    rows = analysis["attempts"]
    predictions = [row for row in rows if row.get("phase") != "cpu-prepare"]
    native_batch = summaries.get(("current-persistent-native-batch", "ubiquitin-76"))
    bir_batch = summaries.get(("bir-graphs-native-batch", "ubiquitin-76"))
    quality = [{"variant": row["variant"], "case": row["case"],
                "per_chain_ca_lddt": row["per_chain_ca_lddt"],
                "median_chain_ca_lddt": row["median_chain_ca_lddt"]}
               for row in analysis["summaries"]]
    model = {
        "model_id": "protenix-v2", "bir_fit": "supported-module-custom-pipeline-required",
        "status": "measured", "adoption_status": "hold-quality-regression",
        "baseline": {
            "image_digest": "sha256:" + CURRENT_IMAGE,
            "checkpoint_sha256": "8f931f9774a396b67033d0e58628e1834f4a1448165e04254b40a780b0c0d599",
            "source_revision": "2475421477ab414b571149ad4a875c390ff8a35d",
            "model_revision": "TMF001/protenix-v2-weights@653edab28103133512575365130916e3fd23ecc3",
            "hardware": {"node": "computeinstance-e00xjaw5jqexvvnpat",
                         "gpu_uuid": "GPU-ca98bcbc-cf06-a4c2-e3bc-c27a0cba95bc",
                         "driver": "580.159.04", "gpu_count": 1},
            "parameters": {"cycles": 10, "diffusion_steps": 200, "samples": 1,
                           "seed": 101, "msa": "none", "templates": False,
                           "rna_msa": False, "native_precision_profile": "bf16"},
            "l40s": "unsupported-by-exact-current-wrapper-SM90-H100-check; no altered baseline",
        },
        "candidate": {"image_digest": "sha256:" + BIR_IMAGE, "public_bir": "0.1.0",
                      "torch": "2.12.0+cu130", "source_kind": "custom-evaluation-prototype",
                      "sampling_rng": "caller-global-stream", "cuda_graph_modules": ["token_transformer"]},
        "comparisons": comparisons,
        "native_sample_batch_comparison": {
            "seeds": [101, 202, 303], "samples_per_seed": 2, "structures_per_request": 6,
            "current": native_batch, "bir": bir_batch,
            "median_request_ratio": (native_batch["median_wall_seconds"] / bir_batch["median_wall_seconds"]
                                     if native_batch and bir_batch else None),
        },
        "attempt_counts": {"predictions": len(predictions),
                           "artifact_and_sequence_valid": sum(row.get("status") == "passed" and row.get("expected_sequence_match") is True for row in predictions),
                           "failed": sum(row.get("status") == "failed" for row in predictions),
                           "cpu_prepare": len(rows) - len(predictions)},
        "quality": {"scientific_equivalence_established": False,
                    "metric": analysis["quality_metric"], "results": quality,
                    "warning": "Low-confidence no-MSA fixtures and artificial homodimer are not a broad quality study. Private-seed cohorts changed RNG semantics and are exploratory."},
        "features": {"preserved": ["native localized-input validation", "upstream featurizer",
                                    "upstream confidence/ranking/CIF writer", "checkpoint identity"],
                     "executed_cohorts": sorted({row.get("variant", "cpu-prepare") for row in rows}),
                     "unverified": ["MSA/templates/RNA/ligands outside current no-MSA profile",
                                    "public MCP/HTTP end-to-end integration", "cancellation/idempotency at gateway"]},
        "snapshot": {"evidence": "../snapshot/protenix/result.json", "fresh_bir_restore_qualified": None,
                     "note": "Snapshot lane owns fresh-pod qualification; CUDA Graph flag is not a process snapshot."},
        "economics": {"monetary_rate_assumption": None,
                      "request_window_accounting": analysis["summaries"],
                      "whole_evaluation_allocation": allocation,
                      "allocation_note": "Request-window cost excludes occupied time outside requests; whole-evaluation bounds include initialization, operator gaps, failures and cleanup. Neither is a saturation-price forecast or scientifically acceptable-result cost."},
        "recommendation": "Do not promote: measured reference-quality regression persists across multiple seeds/samples. Prefer current-model residency while investigating identical-feature-tensor parity. Snapshot success cannot waive this quality failure.",
        "cleanup": {"root_benchmark_pods_and_configmaps_deleted": True,
                    "gpu_memory_released": True, "evidence": "raw/root-protenix-final-gpu.stdout",
                    "snapshot_worker_resources": "independently owned; see snapshot/protenix"},
        "limitations": [
            "Three representative lengths, small repetition counts, no p99/SLO claim.",
            "Initial private-seed BIR and different-GPU prototype cohorts are retained but not primary comparisons.",
            "Public BIR forbids graph capture of Protenix trunk/confidence pairformers because replay can produce NaNs.",
            "Hybrid image has dependency-pin conflicts; exact pip freeze retained, not an official supported Protenix environment.",
            "BIR and native images differ in Torch/CUDA/library versions; this measures the proposed stack, not a single isolated kernel.",
            "No production routing, model or checkpoint promotion.",
        ],
        "evidence": ["analysis.json", "raw/", "bir_server.py", "Dockerfile.candidate",
                     "run_baseline.py", "run_prepared.py", "baseline.yaml", "render_candidate.py"],
    }
    result = {"schema": "fs2.bioir-protenix/v1", "generated_at": datetime.now(timezone.utc).isoformat(),
              "models": [model], "no_production_change": True}
    (HERE / "result.json").write_text(json.dumps(result, indent=2, allow_nan=False) + "\n")
    print(json.dumps({"comparisons": len(comparisons), "cohorts": model["features"]["executed_cohorts"]}))


if __name__ == "__main__":
    main()
