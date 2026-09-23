import json
import sys

import pytest

from runtime_receipt import main, sha
import alanine_runtime_receipt


@pytest.mark.parametrize("complete", [False, True])
def test_receipt_binds_real_result_or_returns_nonzero_for_incomplete_archive(tmp_path, monkeypatch, complete):
    runtime, fixture, campaign = [tmp_path / name for name in ("runtime", "fixture", "campaign")]
    for path in (runtime, fixture, campaign):
        path.mkdir()
    image = "example/worker@sha256:pinned"
    def write(path, value):
        path.write_text(json.dumps(value))
    write(runtime / "pod.json", {"metadata": {"uid": "worker-one"},
        "spec": {"containers": [{"name": "runtime", "image": image}], "nodeName": "node-one",
                 "nodeSelector": {"accelerator.fs2.nebius/pool-id": "pool-one"}},
        "status": {"containerStatuses": [{"name": "runtime", "imageID": image}]}})
    (runtime / "native-identity.txt").write_text("name, uuid, driver_version\nTest GPU, GPU-one, test-driver\n")
    (fixture / "input.tar.gz").write_bytes(b"input fixture identity")
    write(fixture / "provenance.json", {"system": "fixture", "ensemble": "nve", "gpu_mode": "resident", "production_steps": 200000, "repetitions": 1})
    write(fixture / "request.json", {"jobs": [{"id": "rep-1"}]})
    write(campaign / "validation.json", {"successful_repetitions": int(complete), "median_ns_per_day": 1,
        "min_ns_per_day": 1, "max_ns_per_day": 1,
        "repetitions": [{"job": "rep-1", "status": "succeeded", "error": None}] if complete else []})
    if complete:
        (campaign / "rep-1").mkdir()
        write(campaign / "rep-1/result.json", {"status": "succeeded", "error": None, "completed_steps": ["production"],
              "recipe_sha256": "recipe", "files": [{"size_bytes": 10}], "commands": [{"step_id": "production"}]})
    output = tmp_path / "receipt.json"
    monkeypatch.setattr(sys, "argv", ["runtime_receipt", "--runtime", str(runtime), "--image", image,
        "--source-revision", "source", "--case", str(fixture), str(campaign), "--output", str(output)])
    if complete:
        main()
    else:
        with pytest.raises(SystemExit) as error:
            main()
        assert error.value.code == 1
    report = json.loads(output.read_text())
    assert report["status"] == ("passed" if complete else "incomplete")
    assert report["customer_ready"] is False
    row = report["tests"][0]
    if complete:
        assert row["result_sha256"] == sha(campaign / "rep-1/result.json")
    else:
        assert "supplied local archive" in row["error"]
        assert "result_sha256" not in row


@pytest.mark.parametrize("change", [None, "result", "request", "incomplete"])
def test_canonical_receipt_binds_exact_validated_result(tmp_path, monkeypatch, change):
    fixture, campaign = tmp_path / "fixture", tmp_path / "campaign"
    fixture.mkdir()
    (campaign / "rep-1").mkdir(parents=True)
    (fixture / "input.tar.gz").write_bytes(b"synthetic canonical fixture")
    (fixture / "request.json").write_text(json.dumps({"jobs": [{"id": "rep-1"}]}))
    result = campaign / "rep-1/result.json"
    result.write_text(json.dumps({"status": "succeeded"}))
    value = {"status": "passed", "input_sha256": sha(fixture / "input.tar.gz"),
             "request_sha256": sha(fixture / "request.json"), "mode": "dynamics",
             "results": [{"job_id": "rep-1", "status": "passed", "result_sha256": sha(result)}]}
    validation = campaign / "validation.json"
    if change == "result":
        result.write_text(json.dumps({"status": "failed"}))
    elif change == "request":
        (fixture / "request.json").write_text(json.dumps({"jobs": [{"id": "another-job"}]}))
    elif change == "incomplete":
        value["results"] = []
    validation.write_text(json.dumps(value))
    monkeypatch.setattr(alanine_runtime_receipt, "runtime_identity", lambda *args: {"gpu_name": "synthetic test GPU"})
    args = (fixture, campaign, validation, tmp_path, "test/image@sha256:pinned", "test-source")
    if change is not None:
        with pytest.raises(ValueError, match="canonical"):
            alanine_runtime_receipt.receipt(*args)
    else:
        report = alanine_runtime_receipt.receipt(*args)
        assert report["status"] == "passed"
        assert report["tests"][0]["result_sha256"] == sha(result)
        assert report["customer_ready"] is report["gpu_snapshot_qualified"] is False
