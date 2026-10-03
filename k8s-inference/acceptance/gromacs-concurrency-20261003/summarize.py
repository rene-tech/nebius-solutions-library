"""Independently validate native MD results and emit a secret-free test receipt."""

import argparse
import hashlib
import json
import math
import re
import statistics
from collections import Counter
from datetime import datetime
from pathlib import Path


def read(path):
    return json.loads(path.read_text())


def timestamp(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def xvg(path, *, density):
    text = path.read_text()
    legends = re.findall(r'(?m)^@ s\d+ legend "([^"]+)"', text)
    expected = ["Potential", "Total Energy", "Temperature", "Pressure"] + (["Density"] if density else [])
    if legends != expected:
        raise ValueError("Unexpected energy column identities")
    rows = [[float(n) for n in line.split()] for line in text.splitlines()
            if line.strip() and not line.startswith(("#", "@"))]
    if not rows or any(len(r) != len(expected) + 1 or not all(math.isfinite(n) for n in r) for r in rows):
        raise ValueError("Nonfinite/incorrect energy series")
    if any(a[0] >= b[0] for a, b in zip(rows, rows[1:])):
        raise ValueError("Energy time is not strictly increasing")
    return rows


def validate(directory):
    receipt, status, result = (read(directory / name) for name in ("receipt.json", "status.json", "result.json"))
    assert receipt["state"] == "verified"
    assert receipt["idempotency_verified"] is True
    assert result["terminal_status"] == "succeeded"
    assert result["semantic_validation"]["status"] == "passed"
    manifest = read(directory / "output-manifest.json")
    index = next(i for i, e in enumerate(manifest["entries"]) if e["semantic_type"] == "gromacs-workflow-result/v1")
    native = read(directory / f"output-{index:02}.artifact")
    assert native["status"] == "succeeded"
    assert len(native["completed_steps"]) == 13
    assert len(native["commands"]) == 13
    assert all(c["exit_code"] == 0 for c in native["commands"])
    files = directory / "native" / "result-00"
    protocol = read(directory / "protocol.json")
    output_protocol = read(files / "starter-protocol.json")
    assert output_protocol == protocol
    check = (files / "fs2-check-segment-000001.log").read_text()
    atom_count = int(re.search(r"# Atoms\s+(\d+)", check)[1])
    assert atom_count == 6598
    final_frame, final_time = re.search(r"Last frame\s+(\d+)\s+time\s+([\d.]+)", check).groups()
    assert float(final_time) == protocol["production_ps"]
    assert int(final_frame) == protocol["production_ps"] // 10
    production = (files / "production.part0001.log").read_text()
    assert int(re.search(r"ld-seed\s*=\s*(\d+)", production)[1]) == protocol["production_seed"]
    assert int(re.search(r"nsteps\s*=\s*(\d+)", production)[1]) == protocol["production_ps"] * 500
    assert "PP tasks will do non-perturbed short-ranged interactions on the GPU" in production
    assert "PME tasks will do all aspects on the GPU" in production
    assert "Finished mdrun on rank 0" in production
    assert not re.search(r"(?im)^(?:WARNING|Fatal error|LINCS WARNING)", production)
    energies = {}
    for phase in ("nvt", "npt", "production"):
        # GROMACS writes density into the energy record with a barostat, not
        # for this fixed-volume NVT stage. Do not manufacture a sixth column.
        rows = xvg(files / f"{phase}-energy.xvg", density=phase != "nvt")
        expected = protocol["production_ps"] if phase == "production" else 20
        assert rows[0][0] == 0 and rows[-1][0] == expected
        assert len(rows) == expected + 1
        means = [statistics.mean(r[i] for r in rows) for i in range(1, len(rows[0]))]
        assert 285 < means[2] < 315  # Basic runtime sanity, not a convergence test.
        energies[phase] = dict(zip(("potential_kj_mol", "total_energy_kj_mol", "temperature_K", "pressure_bar", "density_kg_m3"), means))
    attempts = [a for stage in status["batch"]["stages"] for a in stage["attempts"]]
    assert all(a["resource_released"] for a in attempts)
    operation = status["operation"]
    artifact_bytes = sum(a["size_bytes"] for a in receipt["verified_artifacts"])
    return {"run": str(directory.parent.name + "/" + directory.name), "operation_id": operation["id"],
            "workload_id": status["batch"]["workload_id"],
            "accepted_at": operation["accepted_at"], "completed_at": operation["completed_at"],
            "admission_to_terminal_seconds": (timestamp(operation["completed_at"]) - timestamp(operation["accepted_at"])).total_seconds(),
            "submit_seconds": receipt["submit_seconds"], "submit_http_status": receipt["submit_http_status"],
            "input_sha256": receipt["input_sha256"], "production_seed": protocol["production_seed"],
            "pool": attempts[-1]["scheduling_admission"]["resolved_pool_id"],
            "attempts": len(attempts), "resources_released": True,
            "production_performance_ns_day": next(c["performance_ns_per_day"] for c in native["commands"] if c["step_id"] == "production"),
            "gpu_snapshot_used": native["gpu_snapshot_used"], "native_checkpoint_generation": native["native_checkpoint_generation"],
            "atoms": atom_count, "trajectory_frames": int(final_frame) + 1, "production_ps": float(final_time),
            "trajectory_sha256": hashlib.sha256((files / "canonical-production.xtc").read_bytes()).hexdigest(),
            "artifact_count": len(receipt["verified_artifacts"]), "artifact_bytes": artifact_bytes,
            "all_13_commands_succeeded": True, "energies": energies, "convergence_claimed": False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("evidence", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    results, failures = [], []
    for directory in sorted(args.evidence.glob("cohort-*/request-*")):
        try:
            results.append(validate(directory))
        except Exception as error:
            failures.append({"run": directory.parent.name + "/" + directory.name,
                             "error_type": type(error).__name__, "reason": str(error)[:200]})
    cluster = [json.loads(line) for line in (args.evidence / "cluster-observations.jsonl").read_text().splitlines()]
    health = [json.loads(line) for line in (args.evidence / "api-availability.jsonl").read_text().splitlines()]
    groups, starts = {}, {}
    for sample in cluster:
        for pod in sample.get("pods", []):
            started = pod.get("containers", {}).get("scientific-stage", {}).get("running", {}).get("startedAt")
            if started:
                candidate = {"pod": pod["pod"], "node": pod["node"], "gpu_container_started_at": started}
                if pod["workload_id"] not in starts or started < starts[pod["workload_id"]]["gpu_container_started_at"]:
                    starts[pod["workload_id"]] = candidate
        for node in sample.get("nodes", []):
            group = groups.setdefault(node["pool"], {"first_seen_ready_nodes": None, "max_ready_nodes": 0})
            ready = sum(n["ready"] for n in sample["nodes"] if n["pool"] == node["pool"])
            if group["first_seen_ready_nodes"] is None:
                group["first_seen_ready_nodes"] = ready
            group["max_ready_nodes"] = max(group["max_ready_nodes"], ready)
    for result in results:
        if result["workload_id"] in starts:
            result.update(starts[result["workload_id"]])
            result["admission_to_gpu_container_seconds"] = (timestamp(result["gpu_container_started_at"]) - timestamp(result["accepted_at"])).total_seconds()
    before = read(args.evidence / "qa-policy-before.json")
    restored = read(args.evidence / "qa-policy-restored.json") if (args.evidence / "qa-policy-restored.json").exists() else None
    report = {"schema": "fs2-internal-gromacs-concurrency-evidence/v1", "tenant": "system", "principal": "qa",
              "requested_concurrency": 16, "cohorts": dict(Counter(r["run"].split("/")[0] for r in results)),
              "validated_requests": len(results), "validation_failures": failures,
              "peak_running_gpu_claims": max((s.get("running_gpu_claims", 0) for s in cluster), default=0),
              "peak_observed_gromacs_devices": max((s.get("observed_gromacs_devices", 0) for s in cluster), default=0),
              "api_probes": len(health), "api_probe_errors": [h for h in health if h.get("http_status") != 200],
              "api_probe_p50_seconds": statistics.median(h["seconds"] for h in health),
              "api_probe_max_seconds": max(h["seconds"] for h in health), "observed_pool_scale": groups,
              "internal_key_policy_restored": restored is not None and all(before.get(k) == restored.get(k) for k in
                  ("max_concurrency", "tenant_id", "principal_id", "models", "scopes", "expires_at", "rate_limit_per_minute", "request_budget", "gpu_budget_seconds")),
              "fixture_notes": ["NVT energy extraction asks for Density but the native fixed-volume NVT energy record has no Density column; the diagnostic is retained and NVT density is not invented.",
                                "The 20 ps equilibration + 1 ns production fixture qualifies execution and artifact integrity, not scientific ensemble convergence.",
                                "Container start, GPU process overlap, API acceptance and completed MD throughput are different measurements."],
              "results": results, "customer_keys_used": False, "customer_identity_binding_tested": False}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({k: v for k, v in report.items() if k != "results"}, indent=2))
    if failures:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
