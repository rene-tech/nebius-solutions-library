from datetime import datetime, timezone
import json
from pathlib import Path

import pytest

import collect_report
from recipes import TPR_SHA256


def test_lifecycle_selection_is_exact_read_only_query():
    operation = "9ac94bdc-f65e-41af-a4c2-12e6d1b98810"
    query = collect_report.lifecycle_query([operation, operation])
    assert query.startswith("SELECT") and query.count(operation) == 1
    assert "s.tenant_id='system'" in query and "s.principal_id='qa'" in query
    assert "LIMIT 97" in query and "fs2_reporting_lifecycle_latest" in query
    for invalid in ([], ["not-an-operation"]):
        with pytest.raises(ValueError):
            collect_report.lifecycle_query(invalid)


def test_observer_freeze_keeps_only_selected_ids_and_complete_prefix(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    stamp = "2026-10-05T16:00:00+00:00"
    rows = [{"observed_at": stamp, "pod": {"metadata": {"name": op, "uid": op, "labels": {
        "fs2.nebius.ai/operation-id": op}}, "spec": {"nodeName": "node-" + op}}} for op in ("ours", "other")]
    (source / "pods.jsonl").write_text("".join(json.dumps(row) + "\n" for row in rows) + '{"partial":')
    result = collect_report.freeze_telemetry(source, tmp_path / "frozen", {"ours"}, datetime.now(timezone.utc))
    assert result[0]["selected_records"] == 1 and result[0]["excluded_partial_bytes"] > 0
    retained = json.loads((tmp_path / "frozen/pods.jsonl").read_text())
    assert retained["pod"]["metadata"]["uid"] == "ours"


def setup_work(tmp_path, monkeypatch):
    history = [{"step_id": "repeat-1", "checkpoint_step": 6000},
               {"step_id": "repeat-1", "checkpoint_step": 10000}]
    proof = {"operation_id": "op", "recipe_sha256": "recipe", "native": {
        "status": "passed", "native_result_sha256": "result", "repeats": [{"repeat": 1, "segments": 2, "steps": 10000}]},
        "remote_checkpoint_result_declaration": {"checkpoint_commit_scope": "remote-companion",
                                                "native_checkpoint_generation": 3, "committed_checkpoint_generation": 3}}
    monkeypatch.setattr(collect_report, "check_public", lambda _: proof)
    manifest = {"state": {"operation_id": "op", "job_id": "benchmark", "generation": 3,
                           "recipe_sha256": "recipe", "commands": history},
                "files": [{"path": "original.tpr", "sha256": TPR_SHA256}]}
    path = tmp_path / "benchmark/attempt-001/checkpoint-00000003.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(manifest))

    class Index:
        def __init__(self):
            self.db = self
            self.records = []

        def execute(self, query, args):
            self.rows = [("attempt", "benchmark")] if "FROM attempts" in query else [
                (json.dumps({**c, "ledger_source": "source", "ledger_attempt_quality": "attributed",
                             "native_mdrun_counter_wall_seconds": 1, "native_mdrun_counter_source": "log"}),)
                for c in history]
            return self

        def fetchall(self):
            return self.rows

        def __iter__(self):
            return iter(self.rows)

        def measure(self, *args):
            self.records.append(args)

    return Index(), proof, path


def test_validated_segment_work_uses_existing_ledger_measurements(tmp_path, monkeypatch):
    index, _, _ = setup_work(tmp_path, monkeypatch)
    result = collect_report.validated_work(index, Path("case"), tmp_path)
    assert result["repeats"][0]["repeated_durable_steps"] == 0
    assert [row[4:6] for row in index.records] == [("durable_interval", [0, 10000]), ("executed_steps", 10000)]


def test_missing_remote_commit_cannot_be_called_durable(tmp_path, monkeypatch):
    index, proof, _ = setup_work(tmp_path, monkeypatch)
    proof["remote_checkpoint_result_declaration"]["committed_checkpoint_generation"] = 2
    with pytest.raises(ValueError, match="remote checkpoint"):
        collect_report.validated_work(index, Path("case"), tmp_path)
    assert not index.records


def test_changed_recovered_recipe_rejected(tmp_path, monkeypatch):
    index, _, path = setup_work(tmp_path, monkeypatch)
    manifest = json.loads(path.read_text())
    manifest["state"]["recipe_sha256"] = "other"
    path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="generation differs"):
        collect_report.validated_work(index, Path("case"), tmp_path)
    assert not index.records


def test_dated_prices_use_existing_model():
    from cost_report import hourly_price, preset_shape
    refs = json.loads((Path(__file__).parent / "cost_references.json").read_text())
    l40s = preset_shape("gpu-l40s-d", "4gpu-128vcpu-768gb", "eu-north1")
    h100 = preset_shape("gpu-h100-sxm", "8gpu-128vcpu-1600gb", "eu-north1")
    assert hourly_price(l40s, 1, refs, "2026-10-05")["allocated_share_per_hour"] == pytest.approx(2.2844)
    assert hourly_price(h100, 8, refs, "2026-10-05")["allocated_share_per_hour"] == 36
    assert hourly_price(h100, 8, refs, "2026-10-06")["allocated_share_per_hour"] is None


def test_delivery_clock_excludes_upload_and_unknown_download():
    status = {"operation": {"id": "op", "accepted_at": "2026-10-05T16:00:00Z",
                            "completed_at": "2026-10-05T16:10:00Z"}}
    native = {"native": {"status": "passed", "repeats": [
        {"simulated_ns": 1, "steps": 500000, "coordinates": {"atoms": 185486}}]}}
    result = collect_report.delivery_work(status, {"operation_id": "op"}, native)
    assert result["server_operation_seconds"] == 600
    assert result["server_operation_ns_per_day"] == 144
    assert result["validated_particle_steps"] == 92743000000
    assert result["artifact_download_seconds"] is None
    status["operation"]["completed_at"] = None
    assert collect_report.delivery_work(status, {"operation_id": "op"}, native)["server_operation_ns_per_day"] is None


def accounting_fixture():
    report = {"operations": [{"operation_id": "op", "occupancy_gpu_seconds": {"lower": 1590, "upper": 1620},
                              "allocated_occupancy_cost": {"lower": 1.9875, "upper": 2.025}}],
              "attempts": [{"operation_id": "op", "attempt_id": "a"}],
              "allocations": [{"operation_id": "op", "attempt_id": "a", "gpu_count": 8,
                               "price": {"allocated_share_per_hour": 36}} for _ in range(2)]}
    rows = [{"operation_id": "op", "attempt_id": "a", "rollup_id": "r", "terminal": True, "reconciled": True,
             "scheduler_occupied_gpu_seconds": 1600, "quality": "application_observed", "data_gaps": ["trace_context_missing"]}]
    return report, rows


def test_durable_clock_reuses_per_gpu_rate_without_second_gang_multiplier():
    report, rows = accounting_fixture()
    result = collect_report.lifecycle_comparisons(report, rows)[0]
    assert result["durable_scheduler_occupied_gpu_seconds"] == 1600
    assert result["durable_allocation_share_cost_usd"] == 2
    assert result["data_gaps"] == [["trace_context_missing"]]


def test_missing_attempt_or_mixed_price_is_unknown_not_zero():
    report, rows = accounting_fixture()
    report["attempts"].append({"operation_id": "op", "attempt_id": "missing"})
    assert collect_report.lifecycle_comparisons(report, rows)[0]["durable_scheduler_occupied_gpu_seconds"] is None
    report["attempts"].pop()
    report["allocations"][1]["price"] = {"allocated_share_per_hour": 18}
    result = collect_report.lifecycle_comparisons(report, rows)[0]
    assert result["durable_scheduler_occupied_gpu_seconds"] == 1600
    assert result["durable_allocation_share_cost_usd"] is None


def test_private_csv_keeps_native_rates_without_inventing_public_comparison(monkeypatch, tmp_path):
    report = {"operations": [{"operation_id": "verified"}, {"operation_id": "unknown"}],
              "public_comparisons": []}
    checked = [{"operation_id": "verified", "status": "validated", "native_output_validation": {
        "native": {"repeats": [{"native_inclusive_ns_per_day": rate} for rate in (200, 220, 210)]}}}]
    captured = []
    monkeypatch.setattr(collect_report, "write_summary_csv", lambda value, path: captured.append(value))
    collect_report.write_private_summary_csv(report, checked, tmp_path / "operations.csv")
    assert report["public_comparisons"] == []
    comparison, unknown = captured[0]["public_comparisons"]
    assert comparison["observed_native_median"] == 210
    assert comparison["public_ns_per_day"]["value"] is None
    assert "no matched public benchmark" in comparison["comparison_status"]
    assert unknown["observed_native_median"] is None
    assert unknown["observed_native_ns_per_day"] == []
