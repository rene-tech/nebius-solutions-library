import copy
import hashlib
import struct
import tarfile

import pytest

from audit_outputs import audit
from fs2_gromacs.files import inventory
from fs2_namd import ENGINE_ID, PARAMETER_SCHEMA, RESULT_SCHEMA
from fs2_namd.contracts import canonical, normalize


def example(data):
    request = normalize({"schema": PARAMETER_SCHEMA, "jobs": [{"id": "rep-1", "steps": [{
        "id": "md", "mode": "dynamics", "config": "run.namd", "steps": 1000,
        "segment_steps": 500, "output_prefix": "md", "expected_outputs": ["md.coor"]}]}]})
    (data / "run.namd").write_text("timestep 2\n")
    for suffix in ("coor", "vel"):
        (data / ("md." + suffix)).write_bytes(struct.pack("<i3d", 1, 1, 2, 3))
    (data / "md.xsc").write_text("# cell\n1000 10 0 0 0 10 0 0 0 10 0 0 0\n")
    result = {"schema": RESULT_SCHEMA, "status": "succeeded", "engine_id": ENGINE_ID,
              "job_id": "rep-1", "completed_steps": ["md"],
              "recipe_sha256": hashlib.sha256(canonical({"request": request, "job": "rep-1", "image": ENGINE_ID})).hexdigest(),
              "files": inventory(data, max_bytes=4096),
              "commands": [{"step_id": "md", "segment": n, "exit_code": 0, "atoms": 1,
                            "configured_first_step": (n - 1) * 500, "checkpoint_step": n * 500,
                            "gpu_mode": "resident"} for n in (1, 2)]}
    return request, result


def test_audit_verifies_actual_request_all_files_and_segment_boundaries(tmp_path):
    request, result = example(tmp_path)
    report = audit(request, result, tmp_path)
    assert report["status"] == "passed" and report["verified_files"] == 4
    assert report["dynamics"][0]["final_step"] == 1000
    changed = copy.deepcopy(request)
    changed["output_prefix"] = "different-submitted-prefix"
    with pytest.raises(ValueError, match="recipe"):
        audit(changed, result, tmp_path)
    result["commands"][1]["configured_first_step"] = 0
    with pytest.raises(ValueError, match="boundaries"):
        audit(request, result, tmp_path)


def test_audit_rejects_missing_or_altered_downloaded_native_output(tmp_path):
    request, result = example(tmp_path)
    (tmp_path / "md.coor").write_bytes(struct.pack("<i3d", 1, 9, 2, 3))
    with pytest.raises(ValueError, match="artifact bytes"):
        audit(request, result, tmp_path)
    (tmp_path / "md.coor").unlink()
    with pytest.raises(ValueError, match="missing"):
        audit(request, result, tmp_path)


def test_audit_rejects_wrong_final_native_step_even_with_consistent_hash(tmp_path):
    request, result = example(tmp_path)
    (tmp_path / "md.xsc").write_text("# cell\n500 10 0 0 0 10 0 0 0 10 0 0 0\n")
    result["files"] = inventory(tmp_path, max_bytes=4096)
    with pytest.raises(ValueError, match="cell disagree"):
        audit(request, result, tmp_path)


def test_audit_rejects_missing_requested_native_output_and_stage(tmp_path):
    request, result = example(tmp_path)
    result["files"] = [entry for entry in result["files"] if entry["path"] != "md.coor"]
    with pytest.raises(ValueError, match="requested native output"):
        audit(request, result, tmp_path)
    result["completed_steps"] = []
    with pytest.raises(ValueError, match="ordered stages"):
        audit(request, result, tmp_path)


def test_audit_independently_compares_immutable_input_bundle_even_when_result_hashes_match(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    request, result = example(data)
    bundle = tmp_path / "input.tar.gz"
    with tarfile.open(bundle, "w:gz") as archive:
        archive.add(data / "run.namd", arcname="run.namd")
    report = audit(request, result, data, bundle)
    assert report["immutable_input_bundle"]["verified_files"] == 1
    (data / "run.namd").write_text("timestep 1\n")
    result["files"] = inventory(data, max_bytes=4096)
    with pytest.raises(ValueError, match="immutable input differs"):
        audit(request, result, data, bundle)
