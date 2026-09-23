import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

PATH = Path(__file__).resolve().parents[3] / "models/molecular-dynamics/materialize_customer_results.py"
SPEC = importlib.util.spec_from_file_location("native_md_customer_results", PATH)
results = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(results)


def fixture(tmp_path, model):
    receipt = tmp_path / "receipt"
    receipt.mkdir()
    (receipt / "status.json").write_text(json.dumps({"operation": {"id": "op-test", "status": "succeeded"}, "batch": {"result_published": True}}))
    (receipt / "receipt.json").write_text(json.dumps({"state": "verified"}))
    params = tmp_path / "parameters.json"
    params.write_text(json.dumps({"jobs": [{"id": "rep-1", "steps": [{"id": "production"}]}]}))
    content = b"synthetic artifact, not scientific evidence\n"
    sha = hashlib.sha256(content).hexdigest()
    result = {"operation_id": "op-test", "job_id": "rep-1", "status": "succeeded", "completed_steps": ["production"], "files": [{"path": "nested/result.txt", "size_bytes": len(content), "sha256": sha}]}
    entries = []
    for index, (data, semantic) in enumerate([(json.dumps(result).encode(), f"{model}-workflow-result/v1"), (content, f"{model}-file/v1")]):
        (receipt / f"output-{index:02d}.artifact").write_bytes(data)
        entries.append({"semantic_type": semantic, "artifact": {"sha256": hashlib.sha256(data).hexdigest(), "size_bytes": len(data)}})
    (receipt / "output-manifest.json").write_text(json.dumps({"entries": entries}))
    return receipt, params


@pytest.mark.parametrize("model", ["lammps", "namd"])
def test_materialized_native_workspace_preserves_bytes_not_scientific_claim(tmp_path, model):
    receipt, params = fixture(tmp_path, model)
    output = tmp_path / "workspace"
    record = results.materialize(model, receipt, params, output)
    assert record["scientific_validation_complete"] is False
    assert (output / "rep-1/data/nested/result.txt").read_bytes() == (receipt / "output-01.artifact").read_bytes()
    assert (output / "rep-1/result.json").read_bytes() == (receipt / "output-00.artifact").read_bytes()
    with pytest.raises(FileExistsError):
        results.materialize(model, receipt, params, output)


def test_incomplete_and_corrupt_customer_receipts_are_not_relabelled_passed(tmp_path):
    receipt, params = fixture(tmp_path, "namd")
    (receipt / "output-01.artifact").write_text("corrupt")
    with pytest.raises(ValueError, match="checksum"):
        results.materialize("namd", receipt, params, tmp_path / "output")
    assert not (tmp_path / "output").exists()
