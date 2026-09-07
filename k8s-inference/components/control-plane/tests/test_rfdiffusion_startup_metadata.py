from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest
from conftest import SOLUTION_ROOT

from fs2_serve.scientific_batch.adapters.common import ScientificAdapterError
from fs2_serve.scientific_batch.adapters.rfdiffusion import _validate_cache_evidence


def observed_cli():
    path = SOLUTION_ROOT / "models/scientific-snapshot/rfdiffusion_observed_cli.py"
    spec = importlib.util.spec_from_file_location("rf_observed_cli_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("backend,restored", [("cuda-criu", True), ("normal-load", False)])
def test_actual_supervisor_observation_preserves_scientific_result(tmp_path: Path, backend: str, restored: bool):
    original = {
        "model_id": "rfdiffusion",
        "status": "succeeded",
        "designs": [{"seed": 8100}],
        "cache_level": {"declared": "artifact-local", "source": "submitter-declared", "gpu_snapshot_used": False},
    }
    (tmp_path / "result.json").write_text(json.dumps(original))
    observed = {"backend": backend, "bundle_id": "selected-rf-snapshot", "manifest_sha256": "a" * 64}
    observed_cli().record_startup(tmp_path, observed)
    result = json.loads((tmp_path / "result.json").read_text())
    assert {key: value for key, value in result.items() if key != "cache_level"} == {
        key: value for key, value in original.items() if key != "cache_level"
    }
    assert result["cache_level"]["gpu_snapshot_used"] is restored
    assert result["cache_level"]["observed_startup"] == observed
    _validate_cache_evidence(result["cache_level"])


def test_selected_policy_or_legacy_claim_is_not_actual_restore():
    native = {"declared": "artifact-local", "source": "submitter-declared", "gpu_snapshot_used": False}
    _validate_cache_evidence(native)
    with pytest.raises(ScientificAdapterError):
        _validate_cache_evidence({**native, "gpu_snapshot_used": True})
    with pytest.raises(ScientificAdapterError):
        _validate_cache_evidence(
            {
                **native,
                "source": "runtime-observed",
                "gpu_snapshot_used": True,
                "observed_startup": {"backend": "normal-load", "bundle_id": "selected-rf", "manifest_sha256": "a" * 64},
            }
        )


def test_failed_native_output_cannot_be_relabelled_successful(tmp_path: Path):
    (tmp_path / "result.json").write_text(json.dumps({"model_id": "rfdiffusion", "status": "failed"}))
    with pytest.raises(ValueError, match="successful original"):
        observed_cli().record_startup(
            tmp_path, {"backend": "cuda-criu", "bundle_id": "rf", "manifest_sha256": "a" * 64}
        )
