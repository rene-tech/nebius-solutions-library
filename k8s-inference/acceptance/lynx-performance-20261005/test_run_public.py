import json

import pytest

from recipes import TPR_SHA256, parameters, sha
from run_public import fixture_identity


def test_fixture_refuses_app_mismatch_and_drift(tmp_path):
    (tmp_path / "input.tar.gz").write_bytes(b"toy")
    (tmp_path / "request.json").write_text(json.dumps(parameters()))
    (tmp_path / "fixture.json").write_text(json.dumps({"tpr_sha256": TPR_SHA256,
        "input_sha256": sha(tmp_path / "input.tar.gz"), "request_sha256": sha(tmp_path / "request.json"),
        "steps": 50000, "repetitions": 3}))
    identity = fixture_identity(tmp_path, "gromacs", "rest")
    assert identity["original_tpr_sha256"] == TPR_SHA256
    with pytest.raises(ValueError, match="App"):
        fixture_identity(tmp_path, "gromacs-mpi", "rest")
    (tmp_path / "input.tar.gz").write_bytes(b"changed")
    with pytest.raises(ValueError, match="changed"):
        fixture_identity(tmp_path, "gromacs", "rest")
