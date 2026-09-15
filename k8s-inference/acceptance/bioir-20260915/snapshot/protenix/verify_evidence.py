#!/usr/bin/env python3
"""Fail closed on missing repeats, model identity, native output or cleanup data."""
import argparse
import json
from control import ROOT, BASE, COHORT, PREFIX, NODE, IMAGE, LABELS, GPU_UUID, DRIVER

assert COHORT in {"xjaw", "y0jt"}, "The interrupted fkt cohort cannot pass full qualification"
parser = argparse.ArgumentParser()
parser.add_argument("--partial", action="store_true")
args = parser.parse_args()
data = json.loads((ROOT / "analysis.json").read_text())
checks = {}
groups = {row["variant"]: row for row in data["summaries"]}
assert groups["restore"]["n"] == 3 and groups["donor"]["n"] == 1
assert groups["normal"]["n"] == (2 if args.partial else 3)
if args.partial:
    assert "fallback" not in groups
    assert (ROOT / "incident/node.json").is_file()
    checks["three_restores_two_controls_interruption_explicit"] = True
else:
    assert groups["fallback"]["n"] == 1
    checks["three_restores_three_controls_plus_fallback"] = True
assert len(data["attempts"]) == (13 if args.partial else 18)
for row in data["attempts"]:
    assert row["status"] == "passed" and row["return_code"] == 0
    assert row["sequence_valid"] and row["graph_verified"]
    assert row["quality"]["finite_coordinates"] and row["quality"]["structures"] == 1
    assert len(row["outputs"]) == 1
    assert row["outputs"][0]["residues"] == [76 if row["case"].startswith("ubiquitin") else 129]
    for phase in row["bioir_phases"]:
        assert phase["cycles"] == 10 and phase["steps"] == 200 and phase["samples"] == 1
        assert phase["seed"] == 101 and phase["sampling_rng"] == "caller-global-stream"
checks["retained_native_and_semantically_valid_predictions"] = len(data["attempts"])
uids = set()
donor_released = json.loads((ROOT / "lifecycle" / (PREFIX + "-donor-released.json")).read_text())["unix"]
for row in data["lifecycle"]:
    assert row["pod_uid"] not in uids
    uids.add(row["pod_uid"])
    pod = json.loads((ROOT / "lifecycle" / (row["pod"] + "-before-delete.json")).read_text())
    assert pod["spec"]["nodeName"] == NODE
    assert all(pod["metadata"]["labels"].get(k) == v for k, v in LABELS.items())
    assert pod["spec"]["containers"][0]["image"] == IMAGE
    assert "--allow-device-remap" not in pod["spec"]["containers"][0]["command"]
    assert int(pod["spec"]["containers"][0]["resources"]["requests"]["nvidia.com/gpu"]) == 1
    if row["variant"] == "restore":
        assert donor_released < row["created_unix"]
        text = (ROOT / "raw" / row["pod"] / "supervisor.log").read_text()
        assert '"status": "passed"' in text and '"mechanism": "cuda-criu-restored"' in text
    released = json.loads((ROOT / "inventory" / (row["pod"] + "-released-gpu.json")).read_text())
    assert released["processes"] == "" and "0 MiB, 0 %" in released["telemetry"]
checks["unique_fresh_pods_donor_deleted_first_same_gpu_no_remap"] = True
capture = data["checkpoint_capture"]
assert capture["status"] == "passed" and len(capture["cuda_pids"]) == 1
assert capture["runtime_identity"]["gpu_uuid"] == GPU_UUID
assert capture["runtime_identity"]["driver_version"] == DRIVER
assert any(probe["pid"] == capture["cuda_pids"][0] and probe["state"] == "running" and probe["returncode"] == 0 for probe in capture["cuda_process_probes"])
donor_ready = json.loads((ROOT / "lifecycle" / (PREFIX + "-donor-ready.json")).read_text())
assert donor_ready["health"]["pid"] == capture["cuda_pids"][0]
checks["actual_gpu_owning_worker_capture"] = True
if not args.partial:
    fallback = (ROOT / "raw" / (PREFIX + "-fallback") / "supervisor.log").read_text()
    assert "snapshot runtime, tools, model, driver, kernel or GPU type differs" in fallback
    assert '"mechanism": "normal-load-fallback"' in fallback
    rejection = json.JSONDecoder().raw_decode(fallback)[0]
    assert rejection["status"] == "failed" and rejection["records"] == []
    assert rejection["runtime_identity"]["model_revision"] == "intentional-mismatch-fallback-test"
    fresh_ready = json.loads((ROOT / "lifecycle" / (PREFIX + "-fallback-ready.json")).read_text())
    worker_log = (ROOT / "raw" / (PREFIX + "-fallback") / "worker.log").read_text()
    ready_events = [json.loads(line) for line in worker_log.splitlines() if line.startswith('{"event": "scientific_worker_ready"')]
    assert ready_events[-1]["pid"] == fresh_ready["health"]["pid"] != donor_ready["health"]["pid"]
    assert ready_events[-1]["model_load_seconds"] == fresh_ready["health"]["model_load_seconds"] != donor_ready["health"]["model_load_seconds"]
    assert worker_log.count("BIOIR_BINDING ") >= 2  # Copied donor log prefix, then genuinely new model binding.
    fallback_rows = [row for row in data["attempts"] if row["pod"] == PREFIX + "-fallback"]
    assert len(fallback_rows) == 2 and all(row["status"] == "passed" for row in fallback_rows)
    record = next(row for row in data["lifecycle"] if row["variant"] == "fallback")
    fallback_audit = {"status": "pass", "identity_rejected_before_restore_commands": True, "actual_restore_commands": rejection["records"], "new_worker_pid": fresh_ready["health"]["pid"], "captured_worker_pid": donor_ready["health"]["pid"], "http_health_not_file_marker": True, "new_model_load_seconds": fresh_ready["health"]["model_load_seconds"], "container_started_unix": record["container_started_unix"], "first_observed_http_ready_unix": record["ready_unix"], "container_to_http_ready_seconds": record["container_to_ready_seconds"], "validated_new_predictions": len(fallback_rows), "cache_state": "Supervisor copies captured executable cache before rejecting identity. Fresh ordinary-load controls have empty executable caches; this single fallback is not a matched cache-only benchmark.", "causal_limit": "Cache reuse is a verified configuration difference, not proof that it alone explains all timing differences.", "log_caution": "worker.log deliberately includes copied donor prefix; last ready event and actual live HTTP health identify the new worker.", "health_construction": "Frozen bir_server.py constructs ProtenixBackend before starting HTTPServer; checkpoint torch.load/load_weights and CUDA synchronize precede backend.ready. No readiness file is consulted.", "evidence": ["raw/" + PREFIX + "-fallback/supervisor.log", "raw/" + PREFIX + "-fallback/worker.log", "lifecycle/" + PREFIX + "-fallback-ready.json", "manifests/app.json", "manifests/source.json"]}
    (ROOT / "fallback-verification.json").write_text(json.dumps(fallback_audit, indent=2) + "\n")
    checks["guarded_identity_failure_and_successful_fallback"] = True
    checks["fallback_new_loaded_worker_not_stale_ready_marker"] = True
else:
    checks["fallback_unrun_not_invented"] = True
assert data["source_sha256"] == json.loads((BASE / "inventory/source-hashes.json").read_text())
checks["frozen_model_and_harness_identity_unchanged"] = True
matched = [row for row in data["paired_outputs"] if row["matched_request_history"] and "-restore-" in row["pod"]]
assert len(matched) == 6
checks["matched_history_numerical_variation_retained_without_parity_claim"] = True
if not args.partial:
    final = json.loads((ROOT / "inventory/matrix-complete-gpu.json").read_text())
    assert final["processes"] == "" and "0 MiB, 0 %" in final["telemetry"]
    checks["replacement_gpu_empty_after_matrix"] = True
else:
    checks["interrupted_normal3_gpu_release_unverified"] = True
result = {"status": "partial-evidence-validated" if args.partial else "pass", "acceptance_pass": not args.partial, "checks": checks, "scientific_parity": "not-qualified", "numerical_parity": "see retained pairwise dispersion; no equivalence inference", "scope": COHORT + " independent cohort only; prior interrupted attempts retained separately"}
result["acceptance_scope"] = "Technical capture/fresh-pod restore protocol only; not a scientific-parity or performance-improvement pass"
(ROOT / "verification.json").write_text(json.dumps(result, indent=2) + "\n")
print(json.dumps(result, indent=2))
