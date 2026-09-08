import hashlib
import json
import os
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from aging.altumage.materialize import fetch
from aging.altumage.runtime import AltumAgeRuntime
from aging.contracts import AltumAgeRequest
from aging.server import create_app


@pytest.fixture(scope="module")
def artifact_root():
    configured = os.getenv("ALTUMAGE_TEST_ARTIFACT_ROOT")
    if not configured:
        pytest.skip(
            "set ALTUMAGE_TEST_ARTIFACT_ROOT after materializing pinned official artifacts"
        )
    root = Path(configured)
    assert (root / "weights.pt").is_file()
    return root


@pytest.fixture(scope="module")
def runtime(artifact_root):
    return AltumAgeRuntime(artifact_root)


def matrix_request(runtime, values=None, *, missing_values="error"):
    if values is None:
        values = [[0.5] * 20318]
    return AltumAgeRequest(
        cpg_sites=runtime.cpgs,
        samples=[
            {"sample_id": f"synthetic-{index}", "beta_values": row}
            for index, row in enumerate(values)
        ],
        missing_values=missing_values,
    )


def test_exact_official_pytorch_vs_original_keras_reference(runtime, artifact_root):
    os.environ.setdefault("TF_CPP_MIN_LOG_LEVEL", "2")
    import tensorflow as tf

    tf.config.threading.set_intra_op_parallelism_threads(1)
    tf.config.threading.set_inter_op_parallelism_threads(1)
    reference = tf.keras.models.load_model(artifact_root / "AltumAge.h5", compile=False)
    rng = np.random.default_rng(20260908)
    values = np.stack(
        [
            runtime.center,
            np.full(20318, 0.5),
            np.clip(runtime.center + rng.normal(0, 0.02, 20318), 0, 1),
        ]
    )
    expected = (
        reference((values - runtime.center) / runtime.scale, training=False)
        .numpy()
        .flatten()
    )
    actual = [
        row["predicted_chronological_age_years"]
        for row in runtime.predict(matrix_request(runtime, values.tolist()))
    ]
    # This is implementation parity, not the model's biological prediction error.
    np.testing.assert_allclose(actual, expected, rtol=0, atol=0.001)


def test_cpg_reordering_preserves_prediction(runtime):
    original = matrix_request(runtime, [np.clip(runtime.center, 0, 1).tolist()])
    reversed_request = original.model_copy(
        update={
            "cpg_sites": list(reversed(original.cpg_sites)),
            "samples": [
                original.samples[0].model_copy(
                    update={
                        "beta_values": list(reversed(original.samples[0].beta_values))
                    }
                )
            ],
        }
    )
    assert runtime.predict(reversed_request) == runtime.predict(original)


def test_explicit_null_imputation_uses_original_scaler_median(runtime):
    values = np.clip(runtime.center, 0, 1).tolist()
    values[0] = None
    with pytest.raises(ValidationError, match="explicit"):
        matrix_request(runtime, [values])
    result = runtime.predict(
        matrix_request(runtime, [values], missing_values="reference_median")
    )[0]
    assert result["imputed_cpg_count"] == 1
    expected = runtime.predict(
        matrix_request(runtime, [np.clip(runtime.center, 0, 1).tolist()])
    )[0]
    assert (
        result["predicted_chronological_age_years"]
        == expected["predicted_chronological_age_years"]
    )


def test_wrong_cpg_identity_is_not_silently_positional(runtime):
    request = matrix_request(runtime)
    sites = list(request.cpg_sites)
    sites[0] = "cg-not-the-trained-probe"
    with pytest.raises(ValueError, match="exactly"):
        runtime.predict(request.model_copy(update={"cpg_sites": sites}))


def test_duplicate_cpg_and_non_beta_value_are_rejected(runtime):
    request = matrix_request(runtime).model_dump()
    request["cpg_sites"][0] = request["cpg_sites"][1]
    with pytest.raises(ValidationError, match="unique"):
        AltumAgeRequest(**request)
    request = matrix_request(runtime).model_dump()
    request["samples"][0]["beta_values"][0] = 1.1
    with pytest.raises(ValidationError):
        AltumAgeRequest(**request)


def test_cpu_endpoint_real_weights_two_distinct_samples(runtime):
    with TestClient(create_app("altumage", runtime_factory=lambda: runtime)) as client:
        request = matrix_request(
            runtime, [[0.5] * 20318, np.clip(runtime.center, 0, 1).tolist()]
        )
        response = client.post("/v1/predict", json=request.model_dump())
        assert response.status_code == 200
        body = response.json()
        assert body["device"] == "cpu"
        assert body["sample_count"] == 2
        assert (
            body["predictions"][0]["predicted_chronological_age_years"]
            != body["predictions"][1]["predicted_chronological_age_years"]
        )
        assert client.get("/v1/health/ready").json()["feature_count"] == 20318
        assert (
            'fs2_model_samples_total{model="altumage"} 2' in client.get("/metrics").text
        )


def test_cuda_request_never_silently_falls_back(artifact_root, monkeypatch):
    import torch

    monkeypatch.setattr(torch.cuda, "is_available", lambda: False)
    with pytest.raises(RuntimeError, match="no silent CPU fallback"):
        AltumAgeRuntime(artifact_root, device="cuda")


def test_materializer_refuses_existing_wrong_artifact_without_network(tmp_path):
    (tmp_path / "weights").write_bytes(b"wrong")
    with pytest.raises(ValueError, match="cached artifact checksum"):
        fetch(tmp_path, "weights", hashlib.sha256(b"right").hexdigest())


def test_manifest_is_exact_and_weight_only_artifact_is_small(artifact_root):
    manifest = json.loads((artifact_root / "manifest.json").read_text())
    assert manifest["feature_count"] == 20318
    assert manifest["artifacts"]["AltumAge.pt"]["size_bytes"] == 2961094
    assert manifest["artifacts"]["weights.pt"]["size_bytes"] < 3_000_000
