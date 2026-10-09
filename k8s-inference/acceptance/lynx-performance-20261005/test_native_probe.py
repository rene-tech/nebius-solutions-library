import json

import native_probe
from recipes import TPR_SHA256, parameters


def fixture(tmp_path, monkeypatch):
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setattr(native_probe, "sha", lambda p: TPR_SHA256)
    monkeypatch.setattr(native_probe.native, "validate_gro", lambda *args: {"finite": True, "atoms": 185486})
    (data / "original.tpr").write_bytes(b"toy-not-customer-input")
    request = parameters(steps=10000)
    commands = []
    for repeat in (1, 2, 3):
        name = f"repeat-{repeat}.log"
        (data / name).write_text("Time: 96.0 12.0 800.0\nPerformance: 144.0\n")
        (data / f"energy{repeat}.xvg").write_text("0 -100 50 -50 310 1\n20 -100 50 -50 310 1\n")
        commands.append({"step_id": f"repeat-{repeat}", "checkpoint_step": 10000,
                         "exit_code": 0, "log": name, "performance_ns_per_day": 144.0})
    result = {"files": [{"path": "original.tpr", "size_bytes": (data / "original.tpr").stat().st_size,
                         "sha256": TPR_SHA256}], "commands": commands, "status": "succeeded",
              "inventory_complete": True, "completed_steps": [s["id"] for s in request["jobs"][0]["steps"]]}
    (tmp_path / "result.json").write_text(json.dumps(result))
    return request, result


def test_validates_exact_repeats_and_counter_semantics(tmp_path, monkeypatch):
    request, _ = fixture(tmp_path, monkeypatch)
    record = native_probe.validate(tmp_path, request, 1, False, False, 0)
    assert record["status"] == "passed"
    assert record["median_native_inclusive_ns_per_day"] == 144
    assert record["customer_path_tested"] is False


def test_rejects_replayed_native_steps(tmp_path, monkeypatch):
    request, result = fixture(tmp_path, monkeypatch)
    result["commands"].append(dict(result["commands"][0]))
    (tmp_path / "result.json").write_text(json.dumps(result))
    record = native_probe.validate(tmp_path, request, 1, False, False, 0)
    assert record["status"] == "failed"
    assert any("continuity" in error for error in record["errors"])


def test_rejects_missing_rank_binding_and_nonfinite_energy(tmp_path, monkeypatch):
    request, _ = fixture(tmp_path, monkeypatch)
    (tmp_path / "data/energy1.xvg").write_text("0 nan 50 -50 310 1\n20 -100 50 -50 310 1\n")
    record = native_probe.validate(tmp_path, request, 2, True, False, 0)
    assert record["status"] == "failed"
    assert any("energy" in error for error in record["errors"])
    assert any("bindings" in error for error in record["errors"])
