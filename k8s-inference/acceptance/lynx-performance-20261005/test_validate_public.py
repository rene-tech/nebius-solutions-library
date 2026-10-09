import json

import pytest

import validate_public
from recipes import TPR_SHA256, sha


def test_public_evidence_uses_same_native_validator_without_copying(tmp_path, monkeypatch):
    result = tmp_path / "result.artifact"
    result.write_text(json.dumps({"commands": [], "recipe_sha256": "r", "files": [],
                                  "operation_id": "op", "status": "succeeded"}))
    data = tmp_path / "native"
    data.mkdir()
    receipt = {"state": "verified", "idempotency_verified": True, "operation_id": "op",
               "benchmark_identity": {"original_tpr_sha256": TPR_SHA256, "interface": "mcp", "model_id": "gromacs-mpi"},
               "verified_artifacts": [{"path": str(result), "size_bytes": result.stat().st_size, "sha256": sha(result)}],
               "native_outputs": {"directories": [{"path": str(data)}]}}
    (tmp_path / "receipt.json").write_text(json.dumps(receipt))
    (tmp_path / "request.json").write_text(json.dumps({"parameters": {"nodes": 2, "gpus_per_node": 8}}))

    def validator(workspace, request, gpu_count, mpi, expected_failure, worker_exit, **kwargs):
        assert gpu_count == 16 and mpi and not expected_failure
        assert kwargs == {"data_dir": data, "result_path": result}
        return {"status": "passed"}

    monkeypatch.setattr(validate_public, "validate", validator)
    assert validate_public.check(tmp_path)["public_transport_verified"]
    result.write_text("changed")
    with pytest.raises(ValueError, match="identity changed"):
        validate_public.check(tmp_path)


def test_rejects_unverified_public_receipt(tmp_path):
    (tmp_path / "receipt.json").write_text('{"state":"running"}')
    with pytest.raises(ValueError, match="already verified"):
        validate_public.check(tmp_path)
