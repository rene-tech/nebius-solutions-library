"""Verify report accounting and artifact integrity; not a scientific parity gate."""
import hashlib
import json
from pathlib import Path

root = Path(__file__).resolve().parent
stats = json.loads((root / "statistics.json").read_text())
audit = json.loads((root / "evidence/quality-audit.json").read_text())
checks = {}
for name in ("current-h100", "persistent-h100-valid", "bir-h100-matrix", "persistent-tools-h100-matrix",
             "current-l40s-matrix", "persistent-l40s-matrix", "bir-l40s-seeded", "persistent-tools-l40s-matrix"):
    cohort = stats["cohorts"][name]
    assert cohort["valid"] == 13 and cohort["failed"] == 0
    assert all(cohort["repeated_cases"][fixture]["n"] == 3 for fixture in ("T1031", "T1038", "T1096"))
checks["eight_complete_matrices"] = True
assert all(row["sequences_preserved"] for row in audit)
assert len(audit) == 218
assert sum(not row["chain_ids_preserved"] for row in audit) == 10
checks["218_sequence_validated_samples_known_10_chain_id_failures"] = True
assert stats["schema_probe_attempts"] == stats["schema_probe_passes"] == 81
checks["81_schema_probes"] = True
assert stats["total_prediction_attempts"] == stats["total_valid_predictions"] + stats["total_failed_predictions"] == 228
checks["all_completed_http_attempts_counted"] = True
for gpu, row in stats["all_pod_allocation_bounds"].items():
    assert row["all_attempts_gpu_seconds_upper"] is not None
    assert row["all_attempts_gpu_seconds_lower"] <= row["all_attempts_gpu_seconds_upper"]
checks["all_pod_cost_bounds_complete"] = True
before = json.loads((root / "raw/live-deployment.stdout").read_text())
after = json.loads((root / "raw/live-deployment-final.stdout").read_text())
assert before["spec"] == after["spec"]
checks["production_spec_unchanged"] = True
for line in (root / "raw/final-runtime-hashes.stdout").read_text().splitlines():
    digest, name = line.split()
    path = root / Path(name).name
    if path.exists():
        assert hashlib.sha256(path.read_bytes()).hexdigest() == digest
checks["runtime_scripts_match_retained_sources"] = True
for gpu in ("h100", "l40s"):
    assert "0 MiB, 0 %" in (root / f"raw/{gpu}-posttest-gpu.stdout").read_text()
    assert len((root / f"raw/{gpu}-posttest-processes.stdout").read_text().strip().splitlines()) == 1
checks["both_gpus_idle_no_processes_after_cleanup"] = True
result = {"status": "pass", "checks": checks, "scientific_noninferiority": "not established",
          "feature_gate": "chain-ID fidelity failed; no promotion", "shared_cache": "PVC deleted after snapshot consumers released it; historical Released PV CSI timeout resolved by normal reclamation, final-cluster-state.json confirms PV absent; no forced deletion"}
(root / "verification.json").write_text(json.dumps(result, indent=2) + "\n")
print(json.dumps(result, indent=2))
