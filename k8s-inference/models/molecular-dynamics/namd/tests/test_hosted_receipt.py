import json

import pytest

from fs2_gromacs.files import digest_file
from fs2_namd import PARAMETER_SCHEMA
from hosted_receipt import receipt


@pytest.fixture
def downloaded(tmp_path):
    request = tmp_path / "request.json"
    request.write_text(json.dumps({"schema": PARAMETER_SCHEMA, "jobs": [{"id": "rep-1", "steps": [{
        "id": "prepare", "config": "prepare.tcl", "mode": "prepare", "expected_outputs": ["prepared.pdb"]}]}]}))
    work = tmp_path / "rep-1"
    work.mkdir()
    result = work / "result.json"
    result.write_text('{"status":"succeeded"}\n')
    (work / "artifact-audit.json").write_text(json.dumps({"job_id": "rep-1", "status": "passed",
        "request_sha256": digest_file(request), "result_sha256": digest_file(result),
        "verified_files": 3, "verified_bytes": 1024, "recipe_sha256": "example-recipe"}))
    validation = tmp_path / "validation.json"
    validation.write_text(json.dumps({"repetitions": [{"job": "rep-1", "status": "succeeded", "trajectories": {"md.dcd": {"frames": 10}}}],
        "median_ns_per_day": 2.0, "min_ns_per_day": 2.0, "max_ns_per_day": 2.0}))
    bundle = tmp_path / "input.tar.gz"
    bundle.write_bytes(b"input-byte-identity-only")
    return request, tmp_path, validation, bundle


def test_hosted_receipt_binds_existing_audits_and_keeps_release_scope_separate(downloaded):
    report = receipt(*downloaded)
    assert report["status"] == "passed"
    assert report["verified_native_files"] == 3 and report["trajectory_frames"] == 10
    assert report["customer_ready"] is False
    assert "submitted input-bundle binding" in report["limitations"][0]
    result = downloaded[1] / "rep-1" / "result.json"
    result.write_text('{"status":"changed-after-audit"}\n')
    with pytest.raises(ValueError, match="exact request/result"):
        receipt(*downloaded)


def test_hosted_receipt_rejects_missing_jobs_and_retains_scientific_failure(downloaded):
    validation = downloaded[2]
    report = json.loads(validation.read_text())
    report["repetitions"][0]["status"] = "failed"
    validation.write_text(json.dumps(report))
    assert receipt(*downloaded)["status"] == "failed"
    report["repetitions"] = []
    validation.write_text(json.dumps(report))
    with pytest.raises(ValueError, match="exact requested jobs"):
        receipt(*downloaded)
