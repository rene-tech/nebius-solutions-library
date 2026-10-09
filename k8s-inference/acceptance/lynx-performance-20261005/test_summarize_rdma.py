import json

import pytest
from collect_report import reference
from summarize_rdma import command_phases, project_samples, validate_source_bindings
from summarize_samples import summarize


def test_request_and_input_archive_must_match_the_terminal_receipt(tmp_path):
    request, archive = tmp_path / "request.json", tmp_path / "input.tar.gz"
    request.write_text('{"nodes": 2}')
    archive.write_bytes(b"retained archive")
    record = {"native_md": {"request_sha256": reference(request)["sha256"],
                            "input_sha256": reference(archive)["sha256"]}}
    validate_source_bindings(record, tmp_path)
    request.write_text('{"nodes": 1}')
    with pytest.raises(ValueError, match="request.json differs"):
        validate_source_bindings(record, tmp_path)


def test_native_projection_reuses_sample_semantics_and_keeps_absent_peak_unknown(tmp_path):
    identities = {"owned": {"spec": {"nodeName": "node"}}}
    capacities = {"node": {"labels": {"nebius.com/nvidia_driver_version": "580"}}}
    rows = []
    for second, usage in ((0, 1000000), (10, 81000000)):
        rows.append({"at": f"2026-10-05T19:00:{second:02d}+00:00", "samples": [
            {"uid": "owned", "node": "node", "pod": "pod", "gpu": {"returncode": 0,
             "stdout": "GPU-a, NVIDIA H100, 50, 1, 123, 200, 1980\n"},
             "cpu": {"returncode": 0, "stdout": f"usage_usec {usage}\nnr_periods {second+1}\n"
                     f"nr_throttled {second//2}\nthrottled_usec 0\n6400000 100000\n4096\n"}},
            {"uid": "unrelated", "node": "other", "pod": "other"}]})
    source, projected = tmp_path / "source.jsonl", tmp_path / "projected.jsonl"
    source.write_text("".join(json.dumps(row) + "\n" for row in rows))
    last = project_samples(source, projected, "native-operation", identities, capacities)
    assert last == {"owned": "2026-10-05T19:00:00+00:00"}
    result = summarize(projected)["pods"]
    assert len(result) == 1 and result[0]["operation_id"] == "native:native-operation"
    assert result[0]["mean_cpu_cores_over_sample_window"] == 8
    assert result[0]["observed_memory_peak_bytes"] is None
    device = result[0]["devices"][0]
    assert device["gpu_utilization_percent"]["sample_mean"] == 50
    assert device["memory_total_mib"]["sample_mean"] is None


def test_changed_sample_identity_cannot_be_joined(tmp_path):
    source = tmp_path / "source.jsonl"
    source.write_text(json.dumps({"at": "2026-10-05T19:00:00Z", "samples": [
        {"uid": "owned", "node": "wrong"}]}) + "\n")
    with pytest.raises(ValueError, match="node differs"):
        project_samples(source, tmp_path / "out", "op", {"owned": {"spec": {"nodeName": "expected"}}}, {})


def test_native_phase_projection_keeps_counter_process_and_missing_phases_distinct(tmp_path):
    (tmp_path / "native.log").write_text("Core t (s) Wall t (s) (%)\nTime: 2560 20 12800\n")
    (tmp_path / "analysis.log").write_text("native trajectory decoded\n")
    commands = [{"step_id": "repeat-1", "wall_seconds": 40, "log": "native.log",
                 "mpi_input_staging": {"wall_seconds": .5, "peers": [{"bytes_transferred": 123}]}},
                {"step_id": "check-1", "wall_seconds": 2, "log": "analysis.log"}]
    request = {"jobs": [{"steps": [{"id": "repeat-1", "command": "mdrun"}, {"id": "check-1", "command": "check"}]}]}
    result = command_phases({"commands": commands}, request, tmp_path, 16, 72)
    assert result["native_mdrun_counter_wall_seconds"]["seconds"]["value"] == 20
    assert result["native_mdrun_counter_wall_seconds"]["gpu_occupancy_seconds"]["value"] == 320
    assert result["native_mdrun_counter_wall_seconds"]["allocated_cost"]["value"] == pytest.approx(.4)
    assert result["analysis_command_seconds"]["seconds"]["value"] == 2
    assert result["mpi_input_staging_bytes"]["value"] == 123
    for name in ("initialization_seconds", "checkpoint_seconds", "export_seconds", "simulation_only_seconds"):
        assert result[name]["value"] is None
